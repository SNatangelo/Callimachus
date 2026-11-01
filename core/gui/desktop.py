# core/gui/desktop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional Callimachus desktop shell.

This module deliberately has no Qt import at module scope.  The desktop starts
the existing CLI driver in a child process and reads only injected projections;
it never changes run, cache, or environment state itself.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
import codecs
import json
from html import escape
import math
import os
import posixpath
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.app.desktop import cache_row_matches_query
from core.app.runtime_paths import (
    application_argv,
    is_frozen,
    resource_root,
    settings_template_file,
    user_data_root,
)
from core.gui.shell.chrome import _MAC_NATIVE_LEADING_RESERVE


_LOCALE_DIR = Path(__file__).with_name("assets") / "locales"
_STOP_ESCALATION_GRACE_MS = 10_000
_SOURCE_SERVICE_CHECK_TIMEOUT_MS = 15_000
_CONSOLE_PAGE_BYTES = 128 * 1024
_CONSOLE_LIVE_WINDOW_BYTES = 128 * 1024
_CONSOLE_EXPORT_CHUNK_BYTES = 64 * 1024
_LLM_ROLE_PREFERENCE_KEY = "llm_role_selection"
_JURY1_CHOOSER_FIRST_SESSION_COMPLETE_KEY = "jury1_chooser_first_session_complete"
_SECRET_SUFFIXES = (
    "_API_KEY", "_API_TOKEN", "_AUTH_TOKEN", "_ACCESS_TOKEN",
    "_SECRET", "_PASSWORD", "_CREDENTIAL",
)
_SECRET_NAMES = {"CITATION_VERIFIER_SIGNING_KEY"}
_SOURCE_VERDICT_COLORS = {
    "supports": "#139a63",
    "partial": "#e3a51c",
    "contradicts": "#e05858",
    "related": "#7b89ac",
    "off_topic": "#8b81aa",
    "non_decidable": "#8992a1",
}


def load_translations(language: str = "en") -> dict[str, str]:
    """Load a complete external catalogue, falling back to English."""
    candidate = _LOCALE_DIR / f"{language}.json"
    fallback = _LOCALE_DIR / "en.json"
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = json.loads(fallback.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in data.items()}


def _document_file_filter(label: str) -> str:
    from core.parse.extract import supported_extensions

    return f"{label} ({' '.join('*' + suffix for suffix in supported_extensions())})"


def _commands_displayed_prefix() -> str:
    if is_frozen():
        return Path(sys.executable).name
    return "py run.py" if sys.platform == "win32" else "python3 run.py"


def _parse_commands_arguments(
    value: str, *, displayed_prefix: str
) -> tuple[list[str] | None, str | None]:
    """Parse CLI arguments and remove at most one optional application prefix."""
    try:
        if sys.platform == "win32":
            lexer = shlex.shlex(value, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            lexer.escape = ""
            tokens = list(lexer)
        else:
            tokens = shlex.split(value, posix=True)
    except ValueError:
        return None, "commands_status_invalid_arguments"

    prefixes = [("py", "run.py"), ("python", "run.py"), ("python3", "run.py")]
    folded_display = displayed_prefix.strip().casefold()
    if folded_display in {"py run.py", "python run.py", "python3 run.py"}:
        display_tokens = tuple(folded_display.split())
    else:
        # A frozen executable name is one argv token even when it contains spaces.
        display_tokens = (displayed_prefix,)
    if display_tokens:
        prefixes.append(display_tokens)

    for prefix in prefixes:
        if len(tokens) >= len(prefix) and tuple(
            token.casefold() for token in tokens[:len(prefix)]
        ) == tuple(token.casefold() for token in prefix):
            tokens = tokens[len(prefix):]
            break

    if not tokens:
        return None, "commands_status_invalid_empty"
    if tokens[0].casefold() == "app":
        return None, "commands_status_invalid_app"
    return tokens, None


def _edge_candidates() -> tuple[Path, ...]:
    roots = (
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA"),
    )
    return tuple(
        Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        for root in roots if root
    )


def _open_local_path(path: str, QtCore, QtGui, *, platform_name: str | None = None) -> bool:
    """Open a local report even when Windows has no default HTML association."""
    target = Path(path)
    if not target.exists():
        return False
    if (platform_name or sys.platform) == "win32":
        try:
            os.startfile(str(target))
            return True
        except OSError:
            pass
        if target.suffix.lower() in {".htm", ".html"}:
            for browser in _edge_candidates():
                if not browser.is_file():
                    continue
                try:
                    subprocess.Popen([str(browser), target.resolve().as_uri()])
                    return True
                except OSError:
                    continue
        return False
    return bool(QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target))))


def _source_evidence_html(item: Any) -> str:
    """Reflow recorded evidence for display without changing its stored text."""
    raw = str(item).replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = []
    for paragraph in re.split(r"\n[ \t]*\n+", raw):
        lines = []
        for physical_line in paragraph.split("\n"):
            physical_line = physical_line.strip()
            if not physical_line:
                continue
            if not lines or re.match(r"(?:[-*+•]|\d+[.)])[ \t]+", physical_line):
                lines.append(physical_line)
            else:
                # Keep a printed hyphen; do not guess whether it belongs to a word.
                joiner = "" if re.search(r"\w-$", lines[-1]) else " "
                lines[-1] += joiner + physical_line
        paragraphs.append("<br>".join(escape(line) for line in lines))
    return "<br><br>".join(paragraphs)


def _source_summary_title(row: Mapping[str, Any], text: Mapping[str, str]) -> str:
    """Return the user-facing source title as plain text for native widgets."""
    from core.app.guided_fetch import source_display_title

    details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
    parsed = details.get("parsed") if isinstance(details.get("parsed"), Mapping) else {}
    has_citation_fields = any(
        key in parsed for key in ("title", "raw_entry", "normalized_raw_entry")
    )
    title = (
        source_display_title(parsed, ref_number=row.get("ref_number"))
        if has_citation_fields
        else row.get("title")
    )
    return str(title).strip() if title not in (None, "") else text["summary_not_recorded"]


def _source_summary_html(
    row: Mapping[str, Any], text: Mapping[str, str], *, dark: bool,
    font_size: int = 16, include_title: bool = True, compact_metadata: bool = False,
) -> str:
    """Render a readable source summary without exposing the raw snapshot payload."""
    foreground, muted, border = (
        ("#e6edf6", "#9faec4", "#34455b") if dark
        else ("#11183c", "#66769c", "#d1def2")
    )
    details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
    parsed = details.get("parsed") if isinstance(details.get("parsed"), Mapping) else {}
    resolved = details.get("resolved") if isinstance(details.get("resolved"), Mapping) else {}
    fetched = details.get("fetch") if isinstance(details.get("fetch"), Mapping) else {}
    pairs = details.get("verification_pairs") if isinstance(details.get("verification_pairs"), list) else []

    def value(item: Any) -> str:
        return escape(str(item).strip()).replace("\n", "<br>") if item not in (None, "") else escape(text["summary_not_recorded"])

    def sequence_value(item: Any) -> str:
        if isinstance(item, Mapping):
            item = (
                item.get("name") or item.get("title") or item.get("value")
                or item.get("author") or item.get("family")
            )
        if isinstance(item, (list, tuple)):
            parts = []
            for entry in item:
                if isinstance(entry, Mapping):
                    entry = (
                        entry.get("name") or entry.get("family")
                        or entry.get("author") or entry.get("given")
                    )
                if entry not in (None, ""):
                    parts.append(str(entry).strip())
            return "; ".join(part for part in parts if part)
        return str(item).strip() if item not in (None, "") else ""

    def metadata_table(rows: Sequence[tuple[str, Any]]) -> str:
        rendered = []
        for label, item in rows:
            if item in (None, ""):
                continue
            if compact_metadata:
                rendered.append(
                    "<tr><td colspan='2' valign='top'>"
                    f"<p class='metadata-label'>{escape(label)}</p>"
                    f"<p class='metadata-value'>{value(item)}</p></td></tr>"
                )
            else:
                rendered.append(
                    "<tr>"
                    f"<td class='metadata-label' valign='top' width='110'>{escape(label)}</td>"
                    f"<td class='metadata-value' valign='top'>{value(item)}</td>"
                    "</tr>"
                )
        return (
            "<table class='metadata' width='100%' cellspacing='0' cellpadding='0'>"
            f"<tbody>{''.join(rendered)}</tbody></table>"
        )

    def section(title: str, body: str, *, heading_level: int = 2) -> str:
        heading_tag = "h3" if heading_level == 3 else "h2"
        return (
            f"<div class='card'><{heading_tag}>{escape(title)}</{heading_tag}>"
            f"{body}</div>"
        )

    phase = str(row.get("phase") or "").casefold()
    phase_label = text.get(f"phase_{phase}", phase.title() or text["summary_not_recorded"])
    status = {
        "completato": text["status_complete"],
        "in_corso": text["status_running"],
        "richiede_intervento": text["status_action_required"],
        "in_attesa": text["status_waiting"],
    }.get(str(row.get("status") or "").casefold(), row.get("status"))
    title = _source_summary_title(row, text)
    identity_rows = []
    received_title = sequence_value(row.get("title"))
    raw_entry = parsed.get("raw_entry") or parsed.get("normalized_raw_entry")
    if received_title and received_title != title:
        identity_rows.append((
            text["summary_citation"]
            if raw_entry and received_title == raw_entry
            else text["summary_received_title"],
            received_title,
        ))
    matched_title = sequence_value(resolved.get("matched_title"))
    if matched_title and matched_title != title:
        identity_rows.append((text["summary_match"], matched_title))
    if raw_entry and raw_entry not in (title, received_title):
        identity_rows.append((text["summary_citation"], raw_entry))
    for key, label_key in (
        ("authors", "summary_authors"),
        ("journal", "summary_publication"),
        ("year", "summary_year"),
        ("volume", "summary_volume"),
        ("issue", "summary_issue"),
        ("pages", "summary_pages"),
    ):
        item = sequence_value(parsed.get(key) or row.get(key))
        if item:
            identity_rows.append((text[label_key], item))
    identifiers = [
        f"{label}: {parsed[key]}"
        for key, label in (("doi", "DOI"), ("pmid", "PMID"), ("isbn", "ISBN"))
        if parsed.get(key) not in (None, "")
    ]
    if identifiers:
        identity_rows.append((text["summary_identifier"], "; ".join(identifiers)))
    if not identity_rows:
        identity_rows.append((text["summary_identity"], text["summary_not_recorded"]))

    expanded_pairs = row.get("_summary_expanded_verification_pairs")
    expanded_pairs = expanded_pairs if isinstance(expanded_pairs, set) else set()
    verdict_items = []
    valid_pairs = [
        (index, pair) for index, pair in enumerate(pairs)
        if isinstance(pair, Mapping)
    ]
    claim_count = sum(bool(pair.get("claim_text") or pair.get("claim_id"))
                      for _index, pair in valid_pairs)
    claim_number = 0
    technical_pair_rows = []
    for pair_index, pair in valid_pairs:
        technical_label = text["summary_verification"]
        if pair.get("status") == "open":
            outcome = text["summary_verification_in_progress"]
        elif (
            pair.get("status") == "accepted"
            and pair.get("terminal_cause") == "jury2_accepted"
            and pair.get("winner_call_id")
            and pair.get("jury1_outcome")
        ):
            outcome = pair["jury1_outcome"]
        else:
            outcome = pair.get("terminal_outcome") or pair.get("status")
        claim = pair.get("claim_text")
        cause = pair.get("terminal_cause")
        summary = ""
        if claim or pair.get("claim_id"):
            claim_number += 1
            technical_label = text["summary_claim"]
            if claim_count > 1:
                technical_label += f" {claim_number}"
            summary = (
                f"<p class='claim-label'>{escape(technical_label)}</p>"
                f"<p class='claim'>{value(claim)}</p>"
            )
        if pair.get("citation_marker"):
            summary += (
                f"<p class='citation'><strong>{escape(text['summary_marker'])}</strong> "
                f"{value(pair['citation_marker'])}</p>"
            )
        pair_key = (
            str(pair.get("claim_id") or ""),
            str(pair.get("ref_id") or row.get("ref_id") or ""),
            str(pair.get("scope") or ""),
        )
        expanded = pair_key in expanded_pairs
        strict_jury2_acceptance = bool(
            pair.get("status") == "accepted"
            and pair.get("terminal_cause") == "jury2_accepted"
            and pair.get("winner_call_id")
            and pair.get("jury1_outcome")
            and isinstance(pair.get("jury2_record"), Mapping)
            and pair["jury2_record"].get("result") == "accepted"
        )
        jury2_record = pair.get("jury2_record")
        jury2_record = jury2_record if isinstance(jury2_record, Mapping) else {}
        jury2_result = str(jury2_record.get("result") or "pending")
        if jury2_result == "accepted" and not strict_jury2_acceptance:
            jury2_result = "pending"
        jury2_label = {
            "accepted": text["summary_jury2_accepted"],
            "rejected": text.get(
                "summary_jury2_rejected", text["summary_jury2_pending"],
            ),
            "not_evaluated": text.get(
                "summary_jury2_not_evaluated", text["summary_jury2_pending"],
            ),
            "pending": text["summary_jury2_pending"],
        }.get(jury2_result, text["summary_jury2_pending"])
        jury1_record = pair.get("jury1_record")
        jury1_record = jury1_record if isinstance(jury1_record, Mapping) else {}
        color = _SOURCE_VERDICT_COLORS.get(str(outcome).casefold())
        outcome_label = text.get(f"atlas_outcome_{outcome}", str(outcome))
        toggle_label = text[
            "summary_collapse_recorded_verdict" if expanded
            else "summary_expand_recorded_verdict"
        ]
        href = f"callimachus://source-verdict/{pair_index}"
        verdict_color = color or ("#9faec4" if dark else "#66769c")
        summary += (
            f"<table class='verdict-control' width='100%' border='0' "
            f"bordercolor='{verdict_color}' cellspacing='0' cellpadding='10'><tr><td>"
            f"<a name='source-verdict-frame-{pair_index}'></a>"
            f"<a href='{href}' aria-expanded='{str(expanded).lower()}'>"
            f"<font color='{verdict_color}'><b>&#9679;&nbsp;{value(outcome_label)}</b></font> "
            f"<font color='{muted}'>{escape(toggle_label)}</font></a>"
            "</td></tr></table>"
        )
        if cause or pair.get("status"):
            technical_pair_rows.append((
                technical_label,
                outcome_label,
                cause,
            ))
        if expanded:
            assessment = (
                f"<p class='jury1-heading'><strong>{escape(text['summary_jury1_explanation'])}</strong></p>"
                f"<p>{value(jury1_record.get('explanation'))}</p>"
            )
            evidence_items = []
            evidence = jury1_record.get("evidence")
            if isinstance(evidence, (list, tuple)):
                for item in evidence:
                    if not isinstance(item, Mapping):
                        continue
                    quotation = item.get("quotation")
                    source_text = item.get("source_text")
                    evidence_text = ""
                    if quotation:
                        evidence_text += (
                            f"<blockquote>{_source_evidence_html(quotation)}</blockquote>"
                        )
                    if source_text and source_text != quotation:
                        evidence_text += (
                            f"<p><small>{escape(text['summary_jury1_evidence'])}</small><br>"
                            f"{_source_evidence_html(source_text)}</p>"
                        )
                    if evidence_text:
                        evidence_items.append(f"<li>{evidence_text}</li>")
            if evidence_items:
                assessment += (
                    f"<p class='evidence-heading'><strong>{escape(text['summary_jury1_evidence'])}</strong></p>"
                    f"<ul class='verdict-evidence'>{''.join(evidence_items)}</ul>"
                )
            jury2 = (
                f"<p class='jury2-heading'><strong>{escape(text['summary_jury2_review'])}</strong></p>"
                f"<p>{escape(jury2_label)}</p>"
            )
            if jury2_record.get("reason"):
                jury2 += (
                    f"<p><small>{escape(text['summary_decision_basis'])}</small><br>"
                    f"{value(jury2_record['reason'])}</p>"
                )
            summary += (
                f"<div class='verdict-disclosure'>{assessment}{jury2}</div>"
            )
        verdict_items.append(
            f"<div class='verdict-pair'><table class='claim-card' width='100%' "
            f"border='0' cellspacing='0' cellpadding='14'><tr><td>"
            f"<a name='source-claim-frame-{pair_index}'></a>{summary}"
            "</td></tr></table><p class='claim-separator'>&nbsp;</p></div>"
        )

    resolution_rows = []
    if resolved:
        lookup_status = str(resolved.get("status") or "").casefold()
        lookup_explanation = text.get(f"summary_lookup_{lookup_status}")
        if lookup_explanation:
            resolution_rows.append((text["summary_resolution"], lookup_explanation))
        resolution_rows.append((
            text["summary_resolution_status"],
            text.get(f"summary_lookup_label_{lookup_status}", resolved.get("status")),
        ))
        for key, label_key in (
            ("via", "summary_method"),
            ("reason", "summary_reason"),
        ):
            if resolved.get(key):
                resolution_rows.append((text[label_key], resolved[key]))
    else:
        resolution_rows.append((text["summary_resolution"], text["summary_resolution_pending"]))

    fetch_rows = []
    if fetched:
        tier = str(fetched.get("tier") or "").casefold()
        tasks = fetched.get("pending_tasks") or ()
        fetch_message_key = (
            f"summary_acquired_{tier}" if tier in {"fulltext", "abstract"}
            else "summary_awaiting_manual" if tasks
            else "summary_no_usable_text"
        )
        fetch_rows.append((text["summary_fetch"], text[fetch_message_key]))
        fetch_rows.append((
            text["summary_text"],
            {
                "fulltext": text["fulltext"],
                "abstract": text["abstract"],
            }.get(tier, text["no_text"]),
        ))
        if isinstance(tasks, (list, tuple)) and tasks:
            fetch_rows.append((text["summary_manual_tasks"], len(tasks)))
        best_source = fetched.get("best_source")
        if isinstance(best_source, Mapping):
            source_label = best_source.get("url")
            if not source_label and str(best_source.get("source_ref") or "").startswith(("https://", "http://")):
                source_label = best_source["source_ref"]
            if not source_label and best_source.get("path"):
                source_label = str(best_source["path"]).replace("\\", "/").rsplit("/", 1)[-1]
            if source_label:
                fetch_rows.append((text["summary_best_source"], source_label))
            confirmation_label = _source_identity_confirmation_label(best_source)
            if confirmation_label:
                fetch_rows.append((text["summary_identity_confirmation"], confirmation_label))
    else:
        fetch_rows.append((text["summary_fetch"], text["summary_fetch_pending"]))

    technical_expanded = bool(row.get("_summary_technical_details_expanded"))
    technical_toggle_label = text[
        "summary_technical_collapse" if technical_expanded
        else "summary_technical_expand"
    ]
    technical_toggle = (
        "<p class='technical-toggle-row'>"
        "<a class='technical-toggle' href='callimachus://source-details/technical' "
        f"aria-expanded='{str(technical_expanded).lower()}'>"
        f"{escape(technical_toggle_label)} "
        f"<span>{'&#9650;' if technical_expanded else '&#9660;'}</span></a></p>"
    )
    technical_content = ""
    if technical_expanded:
        technical_sections = [
            f"<div class='technical-group'><h3>{escape(text['summary_resolution'])}</h3>"
            f"{metadata_table(resolution_rows)}</div>",
            f"<div class='technical-group'><h3>{escape(text['summary_fetch'])}</h3>"
            f"{metadata_table(fetch_rows)}</div>",
        ]
        for claim_label, outcome_label, cause in technical_pair_rows:
            rows = [(text["summary_verification"], outcome_label)]
            if cause:
                rows.append((text["summary_decision_basis"], str(cause).replace("_", " ")))
            technical_sections.append(
                f"<div class='technical-group'><h3>{escape(claim_label)}</h3>"
                f"{metadata_table(rows)}</div>"
            )
        technical_content = (
            f"<div class='technical-disclosure'>{''.join(technical_sections)}</div>"
        )

    claim_content = (
        "".join(verdict_items) if verdict_items
        else f"<p class='no-verdict'>{escape(text['summary_no_verdict'])}</p>"
    )
    heading = f"<h1>{value(title)}</h1>" if include_title else ""
    return f"""
        <html><head><style>
            body {{ color: {foreground}; font-family: 'Segoe UI', sans-serif; font-size: {font_size}px; margin: 0; }}
            h1 {{ font-size: {font_size+4}px; margin: 0 0 10px; }}
            h2 {{ font-size: {font_size}px; margin: 0 0 10px; font-weight: 600; }}
            h3 {{ font-size: {max(13, font_size-1)}px; margin: 0 0 8px; font-weight: 600; }}
            p {{ margin: 0 0 10px; }}
            .intro {{ color: {muted}; font-size: {max(12, font_size-3)}px; margin: 10px 0 24px; }}
            .claim-block {{ margin: 0 0 24px; }}
            .claim-label {{ color: {muted}; font-size: {max(12, font_size-3)}px; font-weight: 600; margin: 0 0 5px; }}
            .claim {{ font-size: {font_size}px; margin: 0 0 10px; }}
            .citation {{ color: {muted}; font-size: {max(12, font_size-3)}px; margin: 0 0 12px; }}
            .verdict-disclosure {{ margin: 16px 0 0; padding: 0; }}
            .jury1-heading {{ color: {'#93bcf2' if dark else '#355f96'}; margin-top: 18px; }}
            .evidence-heading {{ color: {'#d6bc90' if dark else '#826338'}; margin-top: 18px; }}
            .jury2-heading {{ color: {'#c0b1e8' if dark else '#7258a3'}; margin-top: 18px; }}
            .claim-separator {{ font-size: 6px; margin: 0; }}
            .verdict-disclosure p {{ margin: 0 0 8px; }}
            .verdict-disclosure blockquote {{ border-left: 2px solid {border}; margin: 6px 0 12px; padding: 4px 0 4px 12px; font-size: {max(13, font_size-2)}px; }}
            .verdict-evidence {{ margin: 4px 0 10px; padding-left: 18px; }}
            .verdict-evidence li {{ margin: 0 0 10px; }}
            .card {{ border: 0; border-top: 1px solid {border}; padding: 18px 0 0; margin: 26px 0 0; }}
            table.metadata {{ border: 0; background: transparent; margin: 0; }}
            table.metadata td {{ border: 0; vertical-align: top; padding: 4px 0; }}
            .metadata-label {{ color: {muted}; font-size: {max(12, font_size-3)}px; margin: 0 0 4px; }}
            table.metadata td.metadata-label {{ width: 110px; color: {muted}; font-size: {max(12, font_size-3)}px; padding-right: 16px; }}
            table.metadata td.metadata-value {{ color: {foreground}; overflow-wrap: anywhere; }}
            .no-verdict {{ color: {muted}; }}
            .technical-toggle-row {{ margin: 20px 0 0; }}
            .technical-toggle {{ color: {'#7ab4ff' if dark else '#175fe6'}; text-decoration: none; font-weight: 600; }}
            .technical-toggle:hover {{ text-decoration: underline; }}
            .technical-toggle:focus {{ outline: 2px solid {'#7ab4ff' if dark else '#175fe6'}; outline-offset: 2px; }}
            .technical-toggle span {{ font-size: {max(11, font_size-4)}px; }}
            .technical-disclosure {{ border-left: 2px solid {border}; margin: 10px 0 0 2px; padding: 6px 0 0 14px; }}
            .technical-group {{ margin: 0 0 14px; }}
            .technical-group h3 {{ margin: 0 0 6px; }}
            .technical-disclosure p {{ color: {muted}; }}
        </style></head><body>
        {heading}
        <p class="intro">{escape(text['summary_progress'].format(phase=phase_label, status=status or text['summary_not_recorded']))}</p>
        {claim_content}
        {section(text['summary_identity'], metadata_table(identity_rows))}
        {technical_toggle}{technical_content}
        </body></html>
    """


def _source_identity_confirmation_label(source: Mapping[str, Any]) -> str | None:
    confirmation = source.get("identity_confirmation")
    confirmation = confirmation if isinstance(confirmation, Mapping) else {}
    if confirmation.get("status") == "confirmed":
        if confirmation.get("basis") == "controlled_source_provision":
            return "Confirmed by an operator when this exact source was provided."
        return "Confirmed by an operator for this exact source."
    if confirmation.get("status") == "unverified":
        return "An operator reviewed this exact source and kept it unverified."
    if (
        source.get("supplied_by") == "user"
        or str(source.get("supplied_via") or "").startswith("controlled_task_answer:")
    ):
        return "Provided manually; identity has not been separately confirmed."
    return None


def create_desktop_window(
    *,
    language: str | None = None,
    command_builder: Callable[[str, str, str], Sequence[str] | Mapping[str, Any]] | None = None,
    resume_command_builder: Callable[[str], Sequence[str] | Mapping[str, Any]] | None = None,
    snapshot_loader: Callable[[str], Mapping[str, Any]] | None = None,
    overview_loader: Callable[[str], Mapping[str, Any]] | None = None,
    source_page_loader: Callable[[str, int, int], Mapping[str, Any]] | None = None,
    latest_loader: Callable[[], Mapping[str, Any] | None] | None = None,
    latest_resume_loader: Callable[[], Mapping[str, Any] | None] | None = None,
    source_detail_loader: Callable[[str, str], Mapping[str, Any]] | None = None,
    history_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    history_brief_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    history_page_loader: Callable[[int, int], Mapping[str, Any]] | None = None,
    history_detail_loader: Callable[[str], Mapping[str, Any]] | None = None,
    cache_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    cache_page_loader: Callable[..., Mapping[str, Any]] | None = None,
    settings_loader: Callable[[], Mapping[str, Any]] | None = None,
    settings_saver: Callable[[Mapping[str, str]], None] | None = None,
    credential_recovery_state_loader: Callable[[], Mapping[str, Any]] | None = None,
    credential_recovery_callback: Callable[[], Mapping[str, Any]] | None = None,
    run_environment_defaults: Mapping[str, str] | None = None,
    source_contact_loader: Callable[[], Mapping[str, Any]] | None = None,
    source_service_check_callback: Callable[[str, Mapping[str, str]], Mapping[str, Any]] | None = None,
    settings_transfer_callback: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    signing_key_generator: Callable[..., Mapping[str, Any]] | None = None,
    verify_preview_loader: Callable[[str], Mapping[str, Any]] | None = None,
    verify_candidates_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    verify_selection_preview_loader: Callable[[Mapping[str, list[str]], str], Mapping[str, Any]] | None = None,
    llm_preflight_loader: Callable[[], Mapping[str, Any]] | None = None,
    provider_check_callback: Callable[[str, Mapping[str, str]], Mapping[str, Any]] | None = None,
    verify_catalog_preview_loader: Callable[[Mapping[str, Mapping[str, Any]], Mapping[str, list[str]], str], Mapping[str, Any]] | None = None,
    jury_readiness_callback: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
    institutional_access_config_loader: Callable[[], Mapping[str, Any]] | None = None,
    institutional_settings_saver: Callable[[Mapping[str, str]], Any] | None = None,
    institutional_session_factory: Callable[..., Any] | None = None,
    preference_loader: Callable[[], str | None] | None = None,
    preference_saver: Callable[[str], None] | None = None,
    docs_loader: Callable[[], Mapping[str, str] | str] | None = None,
    resume_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    report_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    export_report_callback: Callable[..., Any] | None = None,
    skip_manual_callback: Callable[[str], int] | None = None,
    ocr_pending_count_loader: Callable[[str], int] | None = None,
    verify_fork_options_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    verify_fork_command_builder: Callable[[Mapping[str, Any], Mapping[str, Sequence[str]], Mapping[str, Mapping[str, Any]]], Any] | None = None,
    verify_fork_selector: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Sequence[str]] | None] | None = None,
    delete_cache_callback: Callable[[Sequence[Mapping[str, Any]]], Any] | None = None,
    clear_cache_callback: Callable[[], Any] | None = None,
    library_import_callback: Callable[[list[str], Callable[[dict[str, Any]], None]], list[dict[str, Any]]] | None = None,
    library_save_callback: Callable[[list[dict[str, Any]], Callable[[dict[str, Any]], None]], list[dict[str, Any]]] | None = None,
    library_pending_loader: Callable[[], list[dict[str, Any]]] | None = None,
    library_identity_callback: Callable[[dict[str, Any], str, str], dict[str, Any]] | None = None,
    library_transfer_callback: Callable[[str, str], Mapping[str, Any]] | None = None,
    delete_history_callback: Callable[[Sequence[Mapping[str, Any]]], Any] | None = None,
    stop_session_capture: Callable[[str, int], Any] | None = None,
    stop_session_finalize: Callable[[str, Any], bool] | None = None,
    open_path_callback: Callable[[str], Any] | None = None,
    guided_controller_factory: Callable[[str], Any] | None = None,
    guided_window_factory: Callable[..., Any] | None = None,
    parse_review_controller_factory: Callable[[str], Any] | None = None,
    parse_review_window_factory: Callable[..., Any] | None = None,
    poll_interval_ms: int = 700,
    background_loading: bool = False,
):
    """Create, but do not show, the main desktop window.

    All stateful operations are injected at the boundary.  The default shell is
    useful in CI and documentation builds; the application entry point supplies
    the CLI command and read-only repository projections.
    """
    QtCore, QtGui, QtWidgets = _load_qt()
    from core.gui.shell.chrome import configure_style
    configure_style(QtWidgets)
    from .library_review import create_library_review_dialog, library_reason_text
    open_path_callback = open_path_callback or (
        lambda path: _open_local_path(path, QtCore, QtGui)
    )
    preference_loader = preference_loader or _load_language_preference
    preference_saver = preference_saver or _save_language_preference
    language = language or preference_loader() or "en"
    text = load_translations(language)
    command_builder = command_builder or (lambda _paper, _mode, _level: ())
    resume_command_builder = resume_command_builder or (lambda _run: ())
    snapshot_loader = snapshot_loader or (lambda _run: {})
    overview_loader = overview_loader or snapshot_loader
    latest_loader = latest_loader or (lambda: None)
    history_loader = history_loader or (lambda: ())
    cache_loader = cache_loader or (lambda: ())
    settings_loader = settings_loader or (lambda: {})
    settings_saver = settings_saver or (lambda _updates: None)
    signing_key_generator = signing_key_generator or (lambda: {"created": False})
    delete_history_callback = delete_history_callback or (lambda _rows: None)
    verify_preview_loader = verify_preview_loader or (lambda _level: {})
    verify_candidates_loader = verify_candidates_loader or (lambda: ())
    verify_selection_preview_enabled = (
        verify_selection_preview_loader is not None
        or verify_catalog_preview_loader is not None
    )
    verify_selection_preview_loader = verify_selection_preview_loader or (lambda _roles, _level: {})
    verify_fork_options_loader = verify_fork_options_loader or (lambda: ())
    docs_loader = docs_loader or (lambda: {"README.md": text["no_documentation"]})

    class LoadSignals(QtCore.QObject):
        completed = QtCore.Signal(str, int, object, object)

    class LibrarySignals(QtCore.QObject):
        completed = QtCore.Signal(str, object, object)
        progress = QtCore.Signal(str, object)

    class ReportExportProgressDialog(QtWidgets.QDialog):
        def __init__(self, parent, cancel_callback):
            super().__init__(parent)
            self._cancel_callback = cancel_callback

        def reject(self):
            self._cancel_callback()

        def closeEvent(self, event):
            event.ignore()
            self._cancel_callback()

    class DesktopWindow(QtWidgets.QMainWindow):
        institutional_session_ready = QtCore.Signal(str)

        def __init__(self):
            super().__init__()
            status_bar = self.statusBar()
            status_bar.setVisible(False)
            status_bar.messageChanged.connect(
                lambda message: status_bar.setVisible(bool(message))
            )
            self._run_dir: str | None = None
            self._institutional_retry_pending: str | None = None
            self._institutional_login_deferred = False
            self._institutional_close_pending = False
            self.institutional_session_ready.connect(self._queue_institutional_retry)
            self._background_loading = background_loading
            self._source_page_mode = background_loading and source_page_loader is not None
            self._load_tokens: dict[str, int] = {}
            self._load_futures: dict[str, Any] = {}
            self._loading_kinds: set[str] = set()
            self._report_export_cancel_event: threading.Event | None = None
            self._report_export_close_pending = False
            self._report_export_dialog = None
            self._report_export_spinner = None
            self._report_export_message = None
            self._report_export_cancel_button = None
            self._source_service_check_watchdogs: dict[str, tuple[int, Any]] = {}
            self._settings_environment_names = set(_load_settings_environment_preferences())
            self._load_signals = LoadSignals()
            self._load_signals.completed.connect(self._load_completed)
            self._load_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="callimachus-gui") if (
                background_loading or llm_preflight_loader is not None
                or provider_check_callback is not None or jury_readiness_callback is not None
                or source_service_check_callback is not None or settings_transfer_callback is not None
                or export_report_callback is not None
            ) else None
            self._load_closed = False
            self._credential_recovery_offered = False
            self._library_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="callimachus-library") if library_import_callback or library_save_callback or library_pending_loader or library_identity_callback or library_transfer_callback else None
            self._library_signals = LibrarySignals(self)
            self._library_signals.completed.connect(self._library_completed)
            self._library_signals.progress.connect(self._library_progress_updated)
            self._library_busy = False
            self._library_import_rows: list[dict[str, str]] = []
            self._library_skeleton_effects: list[Any] = []
            self._library_dialog = None
            self._snapshot_loading_run: str | None = None
            self._snapshot_loaded_run: str | None = None
            self._next_snapshot_at = 0.0
            self._pending_finish = None
            self._latest_run_row: Mapping[str, Any] | None = None
            self._latest_load_error: str | None = None
            self._latest_loaded = False
            self._display_run_done = False
            self._display_references_only: bool | None = None
            self._history_rows: list[Mapping[str, Any]] = []
            self._history_requested_dirs: set[str] = set()
            self._cache_rows: list[Mapping[str, Any]] = []
            self._documents: dict[str, str] = {}
            self._source_visible_count = 0
            self._history_visible_count = 0
            self._cache_visible_count = 0
            self._detail_dialog = None
            self._source_detail_next_pending = None
            self._source_detail_next_continuing = False
            self._source_detail_navigation_generation = 0
            self._skeleton_animations: list[Any] = []
            self._run_environment_by_dir: dict[str, dict[str, str]] = {}
            self._process = None
            self._network_permissions_store = None
            self._network_consent_controller = None
            self.network_permissions_button = None
            self._commands_process = None
            self._api_key_actions = {}
            self._service_check_state = {}
            self._browser_cache_clearing = False
            self._browser_cache_exit_done = False
            self._browser_cache_close_pending = False
            self._fetch_cache_clear_pending = False
            self._console_log_path: Path | None = None
            self._console_log_lock = threading.Lock()
            self._console_generation = 0
            self._console_loaded = False
            self._console_capture_error: str | None = None
            self._console_load_error = False
            self._console_loaded_start = 0
            self._console_history_end_offset = 0
            self._console_display_end = 0
            self._console_live_start = 0
            self._console_live_start_offset = 0
            self._console_live_text = ""
            self._console_page_loading = False
            self._console_page_kind: str | None = None
            self._analysis_stdout_decoder = codecs.getincrementaldecoder("utf-8")(
                errors="replace"
            )
            self._analysis_stderr_decoder = codecs.getincrementaldecoder("utf-8")(
                errors="replace"
            )
            self._auto_open_report_on_finish = True
            self._guided_window = None
            self._guided_fetch_available = False
            self._parse_review_window = None
            self._retiring_parse_review_window = None
            self._parse_review_run_dir = None
            self._parse_review_available = False
            self._parse_review_pending = 0
            self._source_rows: list[Mapping[str, Any]] = []
            self._source_paged_rows: list[Mapping[str, Any]] = []
            self._source_paged_total = 0
            self._source_next_offset = 0
            self._source_page_loaded_run: str | None = None
            self._source_query_signature = None
            self._source_query_refresh_failed = False
            self._source_query_refresh_pending = False
            self._source_overview_signature = None
            self._source_page_stale = False
            self._pending_child_source_dir: str | None = None
            self._source_sort_column: int | None = None
            self._source_sort_ascending = True
            self._history_checked_run_dirs: set[str] = set()
            self._cache_checked_keys: set[str] = set()
            self._history_checkbox_pressed_state = None
            self._cache_checkbox_pressed_state = None
            self._dark = bool(QtCore.QSettings("Callimachus", "Callimachus").value("dark_theme", False, type=bool))
            self._console_enabled = bool(QtCore.QSettings("Callimachus", "Callimachus").value("cli_console_enabled", False, type=bool))
            self._spinner_frame = 0
            self._stopping = False
            self._new_analysis_reset_pending = False
            self._stop_escalation_timer = None
            self._forced_stop_process = None
            self._stopped_session_identity = None
            self._cooldown_until: float | None = None
            self._cooldown_activity = ""
            self._last_phase_progress: int | None = None
            self._language = language
            self._selected_language = language
            self._restart_language = None
            self._settings_rows: list[Mapping[str, Any]] = []
            self._settings_search_index: tuple[Mapping[str, Any], ...] = ()
            self._settings_search_targets: dict[str, Any] = {}
            self._settings_search_focus = None
            self._settings_search_reveal_pending = False
            self._settings_search_reveal_applying = False
            self._settings_search_last_scroll_value = 0
            self._settings_editors: dict[str, Any] = {}
            self._settings_initial: dict[str, str] = {}
            self._settings_dirty: dict[str, str] = {}
            self._settings_replacing: set[str] = set()
            self._secret_editors: dict[str, Any] = {}
            self._settings_loaded = False
            self._signing_key_feedback = ""
            self.signing_key_button = None
            self.signing_key_status = None
            self._llm_catalogs: dict[str, Mapping[str, Any]] = {}
            self._jury_readiness_request = None
            self._institutional_dialog = None
            self._institutional_launch_request = None
            self._institutional_session_prepared = False
            self._settings_revision = 0
            self._llm_preflight_busy = False
            self._llm_preflight_status = "idle"
            self._llm_preflight_generation_snapshot: dict[str, int] = {}
            self._provider_check_state: dict[str, dict[str, Any]] = {}
            self._provider_check_generations: dict[str, int] = {}
            self._provider_check_requests: dict[
                str, tuple[int, tuple[tuple[str, str], ...], dict[str, str]]
            ] = {}
            self._provider_check_controls: dict[str, tuple[Any, Any]] = {}
            self._provider_model_editors: dict[str, list[Any]] = {}
            self._page_index = 0
            self._atlas_history_total = 0
            self._atlas_cache_total = 0
            self._atlas_source_cancel = threading.Event()
            self._atlas_query_has_more = False
            self._font_size = self._load_font_size()
            self.setWindowTitle(text["app_title"])
            self.resize(1360, 900)
            self.setMinimumSize(480, 360)
            screen = QtWidgets.QApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.setMinimumSize(min(480, available.width()), min(360, available.height()))
                self.resize(min(1360, available.width()), min(900, available.height()))
            from .guided_fetch import _callimachus_icon
            icon = _callimachus_icon(QtCore, QtGui)
            self.setWindowIcon(icon)
            application = QtWidgets.QApplication.instance()
            if application is not None:
                application.setWindowIcon(icon)
            self._poll_timer = QtCore.QTimer(self)
            self._poll_timer.setInterval(max(100, poll_interval_ms))
            self._poll_timer.timeout.connect(
                self._request_snapshot if background_loading else self._refresh_snapshot
            )
            self._spinner_timer = QtCore.QTimer(self)
            self._spinner_timer.setInterval(250)
            self._spinner_timer.timeout.connect(self._advance_spinner)
            self._spinner_timer.start()
            self._source_query_timer = QtCore.QTimer(self)
            self._source_query_timer.setSingleShot(True)
            self._source_query_timer.timeout.connect(self._load_source_query)
            self._build()
            from core.gui.shell.chrome import install as install_window_chrome
            install_window_chrome(self, QtCore, QtGui, QtWidgets, text)
            self._refresh_static_tabs()
            if llm_preflight_loader is not None:
                self._set_llm_preflight_status("checking")
                QtCore.QTimer.singleShot(0, self._start_llm_preflight)

        def _build(self):
            central = QtWidgets.QWidget(self)
            central.setObjectName("atlasShell")
            layout = QtWidgets.QVBoxLayout(central)
            layout.setContentsMargins(0, 8 if sys.platform == "darwin" else 0, 0, 0)
            layout.setSpacing(0)
            layout.addWidget(self._header(central))
            self.navigation = QtWidgets.QListWidget(central)
            self.navigation.setObjectName("navigationSidebar")
            self.navigation.setIconSize(QtCore.QSize(28, 28))
            self.navigation.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            self.pages = QtWidgets.QStackedWidget(central)
            for label, page in (
                (text["tab_analysis"], self._analysis_tab()),
                (text["tab_history"], self._history_tab()),
                (text["tab_cache"], self._cache_tab()),
                (text["tab_settings"], self._settings_tab()),
                (text["tab_console"], self._commands_tab()),
                (text["tab_info"], self._info_tab()),
            ):
                self.navigation.addItem(label)
                self.pages.addWidget(page)
            self.navigation.currentRowChanged.connect(self._request_page)
            self.navigation.currentRowChanged.connect(lambda _row: self._refresh_navigation_icons())
            self.navigation.setCurrentRow(0)
            nav_shell = QtWidgets.QWidget(central)
            nav_shell.setObjectName("sidebarShell")
            nav_shell.setMinimumWidth(180)
            nav_shell.setMaximumWidth(420)
            nav_layout = QtWidgets.QVBoxLayout(nav_shell)
            nav_layout.setContentsMargins(0, 0, 1, 12)
            nav_layout.addWidget(self.navigation, 1)
            symbols = ("SP_FileIcon", "SP_FileDialogDetailedView", "SP_DirIcon", "SP_FileDialogContentsView")
            for index, symbol in enumerate(symbols):
                self.navigation.item(index).setIcon(self.style().standardIcon(getattr(QtWidgets.QStyle.StandardPixmap, symbol)))
            self._atlas_nav_buttons = []
            for index in (4, 5):
                self.navigation.item(index).setHidden(True)
                button = QtWidgets.QPushButton(text.get("atlas_guide", "Guide") if index == 5 else self.navigation.item(index).text(), nav_shell)
                button.setFlat(True)
                button.setObjectName("sidebarAction")
                button.setProperty("atlasIcon", "console" if index == 4 else "help")
                self._atlas_nav_buttons.append(button)
                button.clicked.connect(lambda _checked=False, page=index: self.navigation.setCurrentRow(page))
                nav_layout.addWidget(button)
                if index == 4:
                    self._console_nav_button = button
                    button.setVisible(self._console_enabled)
            splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, central)
            splitter.setObjectName("globalNavigationSplitter")
            splitter.setChildrenCollapsible(False)
            splitter.setHandleWidth(5)
            splitter.addWidget(nav_shell)
            splitter.addWidget(self.pages)
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setSizes([232, 1128])
            self._atlas_global_splitter = splitter
            from core.gui.shell.navigation import install_sidebar_toggle
            self._atlas_navigation_controller = install_sidebar_toggle(
                splitter, nav_shell, self.navigation_toggle, QtCore, QtWidgets,
                minimum_content_width=820, preferred_width=232, compact_only=True,
                edge_drawer=True, cover_header=True,
            )
            self.navigation.itemClicked.connect(
                lambda _item: self._atlas_navigation_controller.close_overlay()
            )
            for button in self._atlas_nav_buttons:
                button.clicked.connect(self._atlas_navigation_controller.close_overlay)
            layout.addWidget(splitter, 1)
            self.setCentralWidget(central)
            from .atlas_collections import install as install_collections
            install_collections(self, QtCore, QtGui, QtWidgets, text)
            self._apply_theme(self._dark)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            backdrop = getattr(self,"_atlas_jury_backdrop",None)
            if backdrop is not None:
                backdrop.setGeometry(self.centralWidget().rect())
            if hasattr(self, "_atlas_drop_caption"):
                self._atlas_drop_height_floor = max(160, min(280, self.height()-600))
                self.paper_drop.setStyleSheet(
                    "QLabel#paperDropArea { color: transparent; }"
                )
                for name in self._atlas_step_labels:
                    name.setVisible(self.width() >= 1180)
                if self._atlas_options_button.isChecked():
                    self._atlas_reflow_options()
                if hasattr(self, "_atlas_reflow_startup"):
                    QtCore.QTimer.singleShot(0, self._atlas_reflow_startup)

        def _offer_credential_recovery(self):
            if self._load_closed or credential_recovery_state_loader is None:
                return
            try:
                state = credential_recovery_state_loader()
            except (OSError, RuntimeError, ValueError):
                return
            if not state.get("pending") or credential_recovery_callback is None:
                return
            from .shell.messages import compact_message_dialog
            dialog = compact_message_dialog(
                self, QtCore, QtGui, QtWidgets,
                title=text["credential_recovery_title"],
                message=text["credential_recovery_body"], dark=self._dark,
                confirm_label=text["credential_recovery_confirm"],
                cancel_label=text["cancel_action"], object_name="credentialRecoveryDialog",
            )
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return
            try:
                result = credential_recovery_callback()
            except (OSError, RuntimeError, ValueError):
                result = {"pending": 1}
            if self._load_closed:
                return
            if result.get("pending"):
                self.statusBar().showMessage(text["credential_recovery_incomplete"], 12000)
            else:
                self.statusBar().showMessage(text["credential_recovery_complete"], 12000)
            self._queue_load("settings", settings_loader)
            self._refresh_llm_choices()
            self._refresh_llm_status()

        def event(self, event):
            result = super().event(event)
            if event.type() == QtCore.QEvent.Type.Show and not self._credential_recovery_offered and credential_recovery_state_loader is not None:
                self._credential_recovery_offered = True
                QtCore.QTimer.singleShot(0, self._offer_credential_recovery)
            if event.type() == QtCore.QEvent.Type.DevicePixelRatioChange and hasattr(self, "brand_logo"):
                # Qt 6 scales layouts in device-independent units. Re-render
                # the raster wordmark when moving between different-DPI screens.
                self._update_brand_logo()
                screen = self.screen()
                if screen is not None:
                    available = screen.availableGeometry()
                    self.setMinimumSize(min(480, available.width()), min(360, available.height()))
            return result

        def _update_brand_logo(self):
            ratio = self.devicePixelRatioF()
            height = 38
            pixmap = _wordmark(
                QtCore, QtGui, 180, height, ratio, dark=self._dark,
            )
            self.brand_logo.setPixmap(pixmap)
            self._sync_brand_logo_inset()

        def _sync_brand_logo_inset(self):
            inset = 12 if self.navigation_toggle.isHidden() else 0
            self.brand_logo.setContentsMargins(inset, 0, 0, 0)
            self.brand_logo.setFixedSize(180 + inset, 38)

        def _header(self, parent):
            header = QtWidgets.QFrame(parent)
            header.setObjectName("brandHeader")
            row = QtWidgets.QHBoxLayout(header)
            row.setContentsMargins(10, 6, 10, 6)
            self.navigation_toggle = QtWidgets.QToolButton(header)
            self.navigation_toggle.setObjectName("navigationToggle")
            self.navigation_toggle.setProperty("atlasIcon", "menu")
            self.navigation_toggle.setAccessibleName(text["navigation_toggle"])
            self.navigation_toggle.setToolTip(text["navigation_toggle"])
            row.addWidget(self.navigation_toggle)
            logo = QtWidgets.QLabel(header)
            logo.setObjectName("brandLogo")
            logo.setScaledContents(False)
            self.brand_logo = logo
            self._update_brand_logo()
            row.addWidget(logo)
            version = QtWidgets.QLabel((resource_root() / "VERSION").read_text(encoding="utf-8").strip(), header)
            version.setObjectName("mutedText")
            row.addWidget(version)
            row.addStretch()
            self.theme_toggle = QtWidgets.QPushButton(text["theme_dark"], header)
            self.theme_toggle.setObjectName("themeToggle")
            self.theme_toggle.setProperty("atlasIcon", "moon")
            self.theme_toggle.setCheckable(True)
            self.theme_toggle.toggled.connect(self._apply_theme)
            self.theme_toggle.hide()
            self.font_decrease = QtWidgets.QToolButton(header)
            self.font_decrease.setText("A−")
            self.font_decrease.setToolTip(text["font_decrease"])
            self.font_decrease.clicked.connect(lambda: self._change_font_size(-1))
            self.font_decrease.hide()
            self.font_increase = QtWidgets.QToolButton(header)
            self.font_increase.setText("A+")
            self.font_increase.setToolTip(text["font_increase"])
            self.font_increase.clicked.connect(lambda: self._change_font_size(1))
            self.font_increase.hide()
            return header

        def _analysis_tab(self):
            tab = QtWidgets.QWidget()
            outer = QtWidgets.QHBoxLayout(tab)
            outer.setContentsMargins(0, 0, 0, 0)
            splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, tab)
            splitter.setObjectName("analysisSplitter")
            splitter.setChildrenCollapsible(False)
            left_widget = QtWidgets.QWidget(tab)
            left_widget.setObjectName("analysisControlsContent")
            left = QtWidgets.QVBoxLayout(left_widget)
            left.setContentsMargins(0, 0, 4, 0)
            left.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
            config = QtWidgets.QFrame(left_widget)
            config.setObjectName("card")
            form = QtWidgets.QVBoxLayout(config)
            self.paper_drop = QtWidgets.QLabel(text["drop_paper"], config)
            self.paper_drop.setObjectName("paperDropArea")
            self.paper_drop.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.paper_drop.setWordWrap(True)
            self.paper_drop.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
            self.paper_drop.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.paper_drop.setAccessibleName(text["drop_paper"])
            self.paper_drop.setAcceptDrops(True)
            self.paper_drop.installEventFilter(self)
            form.addWidget(self.paper_drop)
            form.addWidget(QtWidgets.QLabel(text["mode"], config))
            modes = QtWidgets.QGridLayout()
            self._analysis_modes_layout = modes
            self._analysis_modes_container = config
            # Start compact; the viewport decides whether two columns fit.
            self._analysis_mode_columns = 1
            self.mode_group = QtWidgets.QButtonGroup(self)
            self.mode_group.setExclusive(True)
            self.mode_buttons = {}
            for index, (key, value) in enumerate((("maximum", "mode_maximum"), ("maximum_fallback", "mode_maximum_fallback"), ("abstract", "mode_abstract"),
                                                  ("standard", "mode_standard"), ("standard_web", "mode_standard_web"))):
                button = QtWidgets.QPushButton(text[value], config)
                button.setCheckable(True)
                button.setProperty("mode", key)
                if key == "standard_web":
                    button.setToolTip(text["mode_standard_web_tip"])
                if key == "standard":
                    button.setChecked(True)
                self.mode_group.addButton(button)
                self.mode_buttons[key] = button
                modes.addWidget(button, index, 0)
            form.addLayout(modes)
            form.addWidget(QtWidgets.QLabel(text["jury2"], config))
            severity = QtWidgets.QHBoxLayout()
            self.jury_group = QtWidgets.QButtonGroup(self)
            self.jury_group.setExclusive(True)
            self.jury_buttons = []
            for level in ("low", "medium", "high"):
                button = QtWidgets.QPushButton(text[f"severity_{level}"], config)
                button.setCheckable(True)
                button.setProperty("jury2_level", level)
                button.setChecked(level == "medium")
                self.jury_group.addButton(button)
                self.jury_buttons.append(button)
                button.toggled.connect(self._refresh_llm_status)
                severity.addWidget(button)
            form.addLayout(severity)
            llm_choices_label = QtWidgets.QLabel(text["llm_choices"], config)
            llm_choices_label.setWordWrap(True)
            form.addWidget(llm_choices_label)
            self.llm_choice_dialog = QtWidgets.QDialog(self)
            self.llm_choice_dialog.setObjectName("juryModelPicker")
            self.llm_choice_dialog.setWindowModality(
                QtCore.Qt.WindowModality.ApplicationModal
                if sys.platform == "darwin"
                else QtCore.Qt.WindowModality.WindowModal
            )
            self.llm_choice_dialog.resize(720, 640)
            picker_layout = QtWidgets.QVBoxLayout(self.llm_choice_dialog)
            self.llm_picker_status = QtWidgets.QLabel(self.llm_choice_dialog)
            self.llm_picker_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.llm_picker_status.setWordWrap(True)
            picker_layout.addWidget(self.llm_picker_status)
            self.llm_discovery_status = None
            self.llm_preflight_button = None
            if llm_preflight_loader is not None:
                discovery_row = QtWidgets.QHBoxLayout()
                self.llm_discovery_status = QtWidgets.QLabel(
                    text["llm_discovery_idle"], self.llm_choice_dialog
                )
                self.llm_discovery_status.setObjectName("llmDiscoveryStatus")
                self.llm_discovery_status.setWordWrap(True)
                self.llm_discovery_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                self.llm_preflight_button = QtWidgets.QPushButton(
                    text["llm_discovery_recheck"], self.llm_choice_dialog
                )
                self.llm_preflight_button.setObjectName("llmDiscoveryRecheck")
                self.llm_preflight_button.clicked.connect(self._start_llm_preflight)
                discovery_row.addWidget(self.llm_discovery_status, 1)
                discovery_row.addWidget(self.llm_preflight_button)
                picker_layout.addLayout(discovery_row)
                self.llm_discovery_details = QtWidgets.QPlainTextEdit(
                    self.llm_choice_dialog
                )
                self.llm_discovery_details.setObjectName("llmDiscoveryDetails")
                self.llm_discovery_details.setReadOnly(True)
                self.llm_discovery_details.setMaximumHeight(104)
                self.llm_discovery_details.setMinimumHeight(56)
                self.llm_discovery_details.setPlaceholderText(text["llm_discovery_idle"])
                picker_layout.addWidget(self.llm_discovery_details)
            else:
                self.llm_discovery_details = None
            self.llm_status = QtWidgets.QLabel(
                text["llm_unavailable"], self.llm_choice_dialog
            )
            self.llm_status.setObjectName("llmStatus")
            self.llm_status.setWordWrap(True)
            picker_layout.addWidget(self.llm_status)
            self.llm_choices = QtWidgets.QTreeWidget(self.llm_choice_dialog)
            self.llm_choices.setColumnCount(3)
            self.llm_choices.setAlternatingRowColors(True)
            self.llm_choices.setObjectName("llmChoices")
            self.llm_role_choices: dict[str, tuple[Any, Any]] = {}
            self._llm_option_labels = {}
            self._llm_last_selected = {}
            self._llm_picker_role = "jury1"
            self._llm_checkbox_cells: dict[Any, Any] = {}
            self._normalizing_llm_roles = False
            self._llm_role_preferences_loaded = False
            self._llm_role_preferences: dict[str, set[str]] | None = None
            self._llm_role_preferences_invalid = False
            self._jury_first_jury1_session = False
            self._jury_initial_mirror_active = False
            self._jury_action_selection_transaction = None
            self._jury_action_selection_active = False
            self.llm_choices.setHeaderLabels((
                text["llm_model"], text["jury1_role"], text["jury2_role"],
            ))
            self.llm_choices.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
            )
            self.llm_choices.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            )
            header = self.llm_choices.header()
            header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
            for column in (1, 2):
                header.setSectionResizeMode(
                    column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
                )
            picker_layout.addWidget(self.llm_choices, 1)
            picker_actions = QtWidgets.QHBoxLayout()
            self.llm_picker_check = QtWidgets.QPushButton(text["provider_check_button"], self.llm_choice_dialog)
            self.llm_picker_check.setObjectName("juryModelsCheck")
            self.llm_picker_check.setEnabled(provider_check_callback is not None or llm_preflight_loader is not None)
            self.llm_picker_check.clicked.connect(self._check_jury_models)
            self.llm_choices.currentItemChanged.connect(self._refresh_jury_picker_check)
            picker_actions.addWidget(self.llm_picker_check)
            picker_actions.addStretch()
            picker_close = QtWidgets.QPushButton(text["close"], self.llm_choice_dialog)
            picker_close.clicked.connect(self.llm_choice_dialog.accept)
            picker_actions.addWidget(picker_close)
            picker_layout.addLayout(picker_actions)
            role_buttons = QtWidgets.QHBoxLayout()
            self.jury_model_buttons = {}
            from .atlante import make_jury_model_button
            for role in ("jury1", "jury2"):
                button = make_jury_model_button(QtCore, QtGui, QtWidgets, config)
                button.setObjectName(f"{role}ModelsButton")
                button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
                button.setMinimumSize(0, 54)
                button.clicked.connect(lambda _checked=False, role=role: self._show_jury_models(role))
                self.jury_model_buttons[role] = button
                button.installEventFilter(self)
                role_buttons.addWidget(button)
            form.addLayout(role_buttons)
            self.institutional_access_status = QtWidgets.QLabel(
                text["institutional_access_optional"], config
            )
            self.institutional_access_status.setObjectName("institutionalAccessStatus")
            self.institutional_access_status.setWordWrap(True)
            institutional_row = QtWidgets.QHBoxLayout()
            institutional_row.addWidget(self.institutional_access_status, 1)
            self.institutional_login_button = QtWidgets.QPushButton(
                text["institutional_login_button"], config
            )
            self.institutional_login_button.setObjectName("institutionalLoginButton")
            self.institutional_login_button.clicked.connect(self._show_institutional_login)
            self.institutional_login_button.setVisible(
                institutional_access_config_loader is not None
            )
            institutional_row.addWidget(self.institutional_login_button)
            form.addLayout(institutional_row)
            controls = QtWidgets.QVBoxLayout()
            self.start_button = QtWidgets.QPushButton(text["start"], config)
            self.start_button.setObjectName("runStartButton")
            self.start_button.setIconSize(QtCore.QSize(24, 24))
            self.start_button.clicked.connect(self._start_analysis)
            self.start_button.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            controls.addWidget(self.start_button, 1)
            self.resume_latest_button = QtWidgets.QPushButton(text["resume_latest"], config)
            self.resume_latest_button.setObjectName("runResumeButton")
            self.resume_latest_button.clicked.connect(self._resume_latest_or_stop)
            self.resume_latest_button.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            controls.addWidget(self.resume_latest_button, 1)
            self.new_analysis_button = QtWidgets.QPushButton(
                text["new_analysis"], config
            )
            self.new_analysis_button.setObjectName("newAnalysisButton")
            self.new_analysis_button.setAccessibleName(text["new_analysis"])
            self.new_analysis_button.setToolTip(text["new_analysis_tip"])
            self.new_analysis_button.setAccessibleDescription(
                text["new_analysis_tip"]
            )
            self.new_analysis_button.clicked.connect(self._new_analysis)
            self.new_analysis_button.setEnabled(False)
            controls.addWidget(self.new_analysis_button)
            form.addLayout(controls)
            self.skip_parse_review = QtWidgets.QCheckBox(
                text["skip_parse_review"], config
            )
            self.skip_parse_review.setObjectName("skipParseReview")
            self.skip_parse_review.setToolTip(text["skip_parse_review_tip"])
            form.addWidget(self.skip_parse_review)
            self.skip_manual_checks = QtWidgets.QCheckBox(text["skip_manual_checks"], config)
            self.skip_manual_checks.setToolTip(text["skip_manual_checks_tip"])
            form.addWidget(self.skip_manual_checks)
            self.open_guided_fetch_button = QtWidgets.QPushButton(text["open_guided_fetch"], config)
            self.open_guided_fetch_button.clicked.connect(
                lambda: self._open_guided_fetch(self._run_dir) if self._run_dir else None
            )
            self.open_guided_fetch_button.setVisible(False)
            form.addWidget(self.open_guided_fetch_button)
            self.open_parse_review_button = QtWidgets.QPushButton(
                text["open_parse_review"], config
            )
            self.open_parse_review_button.setObjectName("openParseReviewButton")
            self.open_parse_review_button.clicked.connect(
                lambda: self._open_parse_review(self._run_dir) if self._run_dir else None
            )
            self.open_parse_review_button.setVisible(False)
            form.addWidget(self.open_parse_review_button)
            self.open_run_report_button = QtWidgets.QPushButton(text["open_report"], config)
            self.open_run_report_button.setObjectName("openRunReport")
            self.open_run_report_button.clicked.connect(self._open_current_report)
            self.open_run_report_button.setVisible(False)
            report_actions = QtWidgets.QHBoxLayout()
            self.report_actions_layout = report_actions
            report_actions.addWidget(self.open_run_report_button, 3)
            self.export_run_report_button = QtWidgets.QPushButton(text["export_report"], config)
            self.export_run_report_button.setObjectName("exportReportButton")
            self.export_run_report_button.setToolTip(text["export_report_tip"])
            self.export_run_report_button.clicked.connect(self._export_current_report)
            self.export_run_report_button.setVisible(False)
            report_actions.addWidget(self.export_run_report_button, 1)
            form.addLayout(report_actions)
            self.phase_progress = QtWidgets.QProgressBar(config)
            self.phase_progress.setObjectName("phaseProgress")
            self.phase_progress.setRange(0, 100)
            self.phase_progress.setValue(0)
            self.phase_progress.setFormat(text["phase_progress"])
            self.phase_progress.setTextVisible(False)
            form.addWidget(self.phase_progress)
            class ElidedActivityLabel(QtWidgets.QLabel):
                def __init__(self, parent, prefix):
                    super().__init__(parent)
                    self._activity_prefix = prefix
                    self.setMinimumWidth(0)
                    self.setSizePolicy(
                        QtWidgets.QSizePolicy.Policy.Ignored,
                        QtWidgets.QSizePolicy.Policy.Preferred,
                    )
                    self.setAlignment(
                        QtCore.Qt.AlignmentFlag.AlignLeft
                        | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )

                def setText(self, value):
                    super().setText(str(value))
                    self.setToolTip(str(value))
                    self.update()

                def paintEvent(self, event):
                    painter = QtGui.QPainter(self)
                    option = QtWidgets.QStyleOption()
                    option.initFrom(self)
                    self.style().drawPrimitive(
                        QtWidgets.QStyle.PrimitiveElement.PE_Widget,
                        option, painter, self,
                    )
                    content = self.text()
                    if self._activity_prefix and content.startswith(self._activity_prefix):
                        content = content[len(self._activity_prefix):].strip()
                    rect = self.contentsRect()
                    content = self.fontMetrics().elidedText(
                        content, QtCore.Qt.TextElideMode.ElideRight, rect.width()
                    )
                    painter.setFont(self.font())
                    painter.setPen(self.palette().color(self.foregroundRole()))
                    painter.drawText(rect, int(self.alignment()), content)

            activity_prefix = text["current_activity"].split("{activity}", 1)[0]
            self.current_activity = ElidedActivityLabel(config, activity_prefix)
            self.current_activity.setObjectName("currentActivity")
            self.current_activity.setAccessibleName(activity_prefix.strip(" :："))
            self.current_activity.setWordWrap(False)
            self.current_activity.setText(
                text["current_activity"].format(activity=text["activity_waiting"])
            )
            form.addWidget(self.current_activity)
            self.phase_progress_percent = QtWidgets.QLabel("0%")
            self.phase_progress_percent.setObjectName("phaseProgressPercent")
            self.phase_progress_percent.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            self.phase_progress_percent.setMinimumWidth(42)
            self.cooldown_status = QtWidgets.QLabel(config)
            self.cooldown_status.setObjectName("cooldownStatus")
            self.cooldown_status.setWordWrap(True)
            self.cooldown_status.hide()
            form.addWidget(self.cooldown_status)
            left.addWidget(config)
            self.phase_label = QtWidgets.QLabel(text["phase_waiting"], left_widget)
            self.phase_label.setObjectName("phaseLabel")
            self.phase_label.setWordWrap(True)
            left.addWidget(self.phase_label)
            left.addStretch()
            left_scroll = QtWidgets.QScrollArea(tab)
            left_scroll.setObjectName("analysisControls")
            left_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            left_scroll.setWidgetResizable(True)
            left_scroll.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            left_scroll.setWidget(left_widget)
            self._analysis_controls_scroll = left_scroll
            self._analysis_checkbox_labels = (
                (
                    self.skip_parse_review,
                    self.skip_parse_review.text().replace("\r\n", " ").replace("\r", " ").replace("\n", " "),
                ),
                (
                    self.skip_manual_checks,
                    self.skip_manual_checks.text().replace("\r\n", " ").replace("\r", " ").replace("\n", " "),
                ),
            )
            self._analysis_checkbox_rewrap_pending = False
            left_scroll.viewport().installEventFilter(self)
            for checkbox, _full_text in self._analysis_checkbox_labels:
                checkbox.installEventFilter(self)
            for button in self.mode_buttons.values():
                button.installEventFilter(self)
            left_scroll.setMinimumWidth(361)
            splitter.addWidget(left_scroll)
            right_widget = QtWidgets.QWidget(splitter)
            right = QtWidgets.QVBoxLayout(right_widget)
            right.setContentsMargins(0, 0, 0, 0)
            filters = QtWidgets.QHBoxLayout()
            self.source_search = QtWidgets.QLineEdit(tab)
            self.source_search.setPlaceholderText(text["search_sources"])
            self.source_search.textChanged.connect(self._reset_source_page)
            filters.addWidget(self.source_search, 1)
            self.source_filter = QtWidgets.QComboBox(tab)
            self.source_filter.addItems((text["all"], text["fulltext"], text["abstract"], text["no_text"]))
            self.source_filter.currentTextChanged.connect(self._reset_source_page)
            filters.addWidget(self.source_filter)
            self.source_phase_filter = QtWidgets.QComboBox(tab)
            for label, value in (
                (text["all"], ""),
                (text["phase_parse"], "parse"),
                (text["phase_resolve"], "resolve"),
                (text["phase_fetch"], "fetch"),
                (text["phase_verify"], "verify"),
            ):
                self.source_phase_filter.addItem(label, value)
            self.source_phase_filter.currentTextChanged.connect(self._reset_source_page)
            filters.addWidget(self.source_phase_filter)
            right.addLayout(filters)
            self.source_table = QtWidgets.QTableWidget(0, 9, tab)
            self.source_table.setObjectName("sourceTable")
            self.source_table.setHorizontalHeaderLabels((text["source_number"], text["source"], text["source_phase"], text["source_status"], text["text_availability"], text["risk_signal"], text["review_labels"], text["result"], text["details"]))
            self.source_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            self.source_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.source_table.setAlternatingRowColors(True)
            self.source_table.verticalHeader().setVisible(False)
            source_header = self.source_table.horizontalHeader()
            source_header.setSectionsClickable(True)
            source_header.setSortIndicatorShown(True)
            source_header.sectionClicked.connect(self._sort_sources)
            source_header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            source_header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            for index in range(2, 9):
                self.source_table.horizontalHeader().setSectionResizeMode(index, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            from core.gui.shell.resize import install_table_resize
            install_table_resize(
                self.source_table, QtCore, QtWidgets, stretch_last_section=True
            )
            self.source_table.cellDoubleClicked.connect(self._show_source_details)
            self.source_table.cellClicked.connect(
                lambda row, column: (
                    self._show_source_details(row, column) if column == 8 else None
                )
            )
            self.source_stack = QtWidgets.QStackedWidget(tab)
            self.source_stack.addWidget(self.source_table)
            self.source_stack.addWidget(self._skeleton_widget(tab))
            from .atlante import empty_state
            self.source_stack.addWidget(empty_state(tab, QtCore, QtWidgets, text["atlas_empty_sources"]))
            right.addWidget(self.source_stack, 1)
            self.source_more_button = QtWidgets.QPushButton(text["show_more"], tab)
            self.source_more_button.setObjectName("loadMoreStrip")
            self.source_more_button.setProperty("analysisSourceFooter", True)
            self.source_more_button.clicked.connect(self._show_more_sources)
            self.source_more_button.hide()
            right.addWidget(self.source_more_button)
            self.source_table.verticalScrollBar().valueChanged.connect(
                lambda _value: self._load_more_near_bottom("source")
            )
            splitter.addWidget(right_widget)
            splitter.setStretchFactor(0, 2)
            splitter.setStretchFactor(1, 5)
            splitter.setSizes([350, 810])
            outer.addWidget(splitter)
            from .atlante import install_analysis
            install_analysis(self, tab, right_widget, QtCore, QtWidgets, text)
            from .atlante import install_source_filters
            install_source_filters(self, QtCore, QtWidgets, text, source_page_loader)
            from .jury_chooser import install_jury_chooser
            install_jury_chooser(self, QtCore, QtGui, QtWidgets, text)
            self.llm_choice_dialog.resize(1000, 840)
            self._refresh_llm_choices()
            self._refresh_llm_status()
            self._update_run_controls()
            return tab

        def _history_tab(self):
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            layout.setContentsMargins(24, 18, 24, 18)
            heading = QtWidgets.QLabel(text["tab_history"], tab)
            heading.setObjectName("pageTitle")
            layout.addWidget(heading)
            self.history_table = self._table(tab, ("", text["history_paper"], text["history_verdict"], text["history_outcome"], text["history_coverage"], text["history_complete"]))
            self.history_table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
            self.history_table.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
            self.history_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
            self.history_table.setColumnWidth(0, 32)
            self.history_table.itemChanged.connect(self._history_item_changed)
            self.history_table.cellPressed.connect(self._remember_history_checkbox_state)
            self.history_table.cellClicked.connect(self._toggle_history_checkbox_cell)
            self.history_table.cellDoubleClicked.connect(self._open_history_row)
            self.history_stack = QtWidgets.QStackedWidget(tab)
            self.history_stack.addWidget(self.history_table)
            self.history_stack.addWidget(self._skeleton_widget(tab))
            from .atlante import empty_state
            self.history_stack.addWidget(empty_state(tab, QtCore, QtWidgets, text["atlas_empty_history"]))
            layout.addWidget(self.history_stack, 1)
            self.history_more_button = QtWidgets.QPushButton(text["show_more"], tab)
            self.history_more_button.setObjectName("loadMoreStrip")
            self.history_more_button.clicked.connect(self._show_more_history)
            self.history_more_button.hide()
            layout.addWidget(self.history_more_button)
            self.history_table.verticalScrollBar().valueChanged.connect(
                lambda _value: self._load_more_near_bottom("history")
            )
            actions_widget = QtWidgets.QWidget(tab)
            self.history_actions_widget = actions_widget
            actions_widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            actions = QtWidgets.QGridLayout(actions_widget)
            actions.setContentsMargins(0, 0, 0, 0)
            self.history_select_all = QtWidgets.QCheckBox(text["select_all"], tab)
            self.history_select_all.checkStateChanged.connect(self._set_all_history_checked)
            actions.addWidget(self.history_select_all, 0, 0)
            resume = QtWidgets.QPushButton(text["resume"], tab)
            resume.setObjectName("historyResumeButton")
            resume.clicked.connect(self._resume_selected_history)
            self.history_resume_button = resume
            self.history_resume_button.setEnabled(False)
            parse_review = QtWidgets.QPushButton(text["open_parse_review"], tab)
            parse_review.setObjectName("historyParseReviewButton")
            parse_review.clicked.connect(self._open_selected_history_parse_review)
            parse_review.setVisible(False)
            self.history_parse_review_button = parse_review
            self.history_table.itemSelectionChanged.connect(
                self._update_history_actions
            )
            report = QtWidgets.QPushButton(text["regenerate_report"], tab)
            report.setObjectName("historyRegenerateReport")
            report.clicked.connect(self._regenerate_report)
            self.history_report_button = report
            self.history_report_button.setEnabled(False)
            open_report = QtWidgets.QPushButton(text["open_report"], tab)
            open_report.setObjectName("historyOpenReport")
            open_report.setProperty("primaryAction", True)
            open_report.clicked.connect(self._open_selected_history_report)
            self.history_open_report_button = open_report
            self.history_open_report_button.setEnabled(False)
            export_report = QtWidgets.QPushButton(text["export_report"], tab)
            export_report.setObjectName("historyExportReportButton")
            export_report.setToolTip(text["export_report_tip"])
            export_report.clicked.connect(self._export_selected_history_report)
            self.history_export_report_button = export_report
            export_report.setEnabled(False)
            verify_fork = QtWidgets.QPushButton(text["rerun_verify"], tab)
            verify_fork.setObjectName("historyVerifyForkButton")
            verify_fork.clicked.connect(self._fork_selected_history_verify)
            self.history_verify_fork_button = verify_fork
            self.history_verify_fork_button.setEnabled(False)
            delete = QtWidgets.QPushButton(text["delete_selected_runs"], tab)
            delete.clicked.connect(self._delete_selected_history)
            self.history_delete_button = delete
            self.history_delete_button.setEnabled(False)
            actions.addWidget(delete, 0, 1)
            delete_run = QtWidgets.QPushButton(text["delete_selected_run"], tab)
            delete_run.setObjectName("dangerButton")
            delete_run.clicked.connect(self._delete_selected_history_run)
            self.history_delete_run_button = delete_run
            self.history_delete_run_button.setEnabled(False)
            actions.setColumnStretch(2, 1)
            actions.addWidget(resume, 1, 0)
            actions.addWidget(parse_review, 1, 2)
            actions.addWidget(report, 1, 3)
            actions.addWidget(verify_fork, 1, 4)
            actions.addWidget(open_report, 1, 1)
            actions.addWidget(export_report, 1, 5)
            self.history_report_folder_button = QtWidgets.QPushButton(text["atlas_report_folder"], tab)
            self.history_report_folder_button.setObjectName("historyReportFolder")
            self.history_report_folder_button.setEnabled(False)
            self.history_report_folder_button.clicked.connect(self._open_selected_report_folder)
            actions.addWidget(self.history_report_folder_button, 2, 4, 1, 2)
            actions.addWidget(
                delete_run, 3, 0, 1, 6,
                QtCore.Qt.AlignmentFlag.AlignRight,
            )
            actions_scroll = QtWidgets.QScrollArea(tab)
            self.history_actions_scroll = actions_scroll
            actions_scroll.setObjectName("historyActions")
            actions_scroll.setWidgetResizable(True)
            actions_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            actions_scroll.setWidget(actions_widget)
            layout.addWidget(actions_scroll)
            return tab

        def _cache_tab(self):
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            layout.setContentsMargins(24, 18, 24, 18)
            heading = QtWidgets.QLabel(text["tab_cache"], tab)
            heading.setObjectName("pageTitle")
            layout.addWidget(heading)
            self.cache_summary = QtWidgets.QLabel("", tab)
            self.cache_summary.setObjectName("cacheSummary")
            self.cache_summary.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            layout.addWidget(self.cache_summary)
            library_actions = QtWidgets.QHBoxLayout()
            import_button = QtWidgets.QPushButton(text["library_import"], tab)
            import_button.setObjectName("libraryImport")
            import_button.setEnabled(library_import_callback is not None)
            import_menu = QtWidgets.QMenu(import_button)
            import_menu.setObjectName("libraryImportMenu")
            import_menu.addAction(text["library_import_documents"], self._choose_library_documents)
            import_menu.addAction(text["library_import_folder"], self._choose_library_folder)
            import_button.setMenu(import_menu)
            self.library_import_button = import_button
            library_actions.addWidget(import_button)
            pending_button = QtWidgets.QPushButton(text["library_pending_review"], tab)
            pending_button.setObjectName("libraryPendingReview")
            pending_button.setEnabled(library_pending_loader is not None)
            pending_button.clicked.connect(self._review_library_pending)
            self.library_pending_button = pending_button
            library_actions.addWidget(pending_button)
            library_actions.addStretch()
            layout.addLayout(library_actions)
            archive_actions = QtWidgets.QHBoxLayout()
            self.library_archive_import_button = QtWidgets.QPushButton(text["library_archive_import"], tab)
            self.library_archive_import_button.setObjectName("libraryArchiveImport")
            self.library_archive_import_button.clicked.connect(lambda: self._choose_library_archive("import"))
            self.library_archive_export_button = QtWidgets.QPushButton(text["library_archive_export"], tab)
            self.library_archive_export_button.setObjectName("libraryArchiveExport")
            self.library_archive_export_button.clicked.connect(lambda: self._choose_library_archive("export"))
            for button in (self.library_archive_import_button, self.library_archive_export_button):
                button.setEnabled(library_transfer_callback is not None)
                archive_actions.addWidget(button)
            archive_actions.addStretch()
            layout.addLayout(archive_actions)
            class LibraryProgress(QtWidgets.QLabel):
                def setText(self, value):
                    super().setText(value)
                    self.setVisible(bool(value))
            self.library_progress = LibraryProgress("", tab)
            self.library_progress.hide()
            self.library_progress.setObjectName("libraryProgress")
            self.library_progress.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.library_progress.setWordWrap(True)
            layout.addWidget(self.library_progress)
            self.library_progress_bar = QtWidgets.QProgressBar(tab)
            self.library_progress_bar.setObjectName("libraryImportProgressBar")
            self.library_progress_bar.setFormat(text["library_progress_count"])
            self.library_progress_bar.setAccessibleName(text["library_import"])
            self.library_progress_bar.hide()
            layout.addWidget(self.library_progress_bar)
            self.cache_search = QtWidgets.QLineEdit(tab)
            self.cache_search.setPlaceholderText(text["search_sources"])
            self.cache_search.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            self.cache_search.textChanged.connect(self._reset_cache_page)
            filters = QtWidgets.QHBoxLayout()
            filters.addWidget(self.cache_search, 1)
            self.cache_filter = QtWidgets.QComboBox(tab)
            self.cache_filter.addItems(
                (text["all"], text["fulltext"], text["abstract"], text["no_text"])
            )
            self.cache_filter.currentTextChanged.connect(self._reset_cache_page)
            filters.addWidget(self.cache_filter)
            layout.addLayout(filters)
            self.cache_table = self._table(tab, ("", text["cache_paper"], text["text_availability"], text["cache_updated"]))
            self.cache_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
            self.cache_table.setColumnWidth(0, 32)
            self.cache_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            self.cache_table.itemChanged.connect(self._cache_item_changed)
            self.cache_table.itemSelectionChanged.connect(self._sync_cache_select_all)
            self.cache_table.cellPressed.connect(self._remember_cache_checkbox_state)
            self.cache_table.cellClicked.connect(self._toggle_cache_checkbox_cell)
            self.cache_stack = QtWidgets.QStackedWidget(tab)
            self.cache_stack.addWidget(self.cache_table)
            self.cache_stack.addWidget(self._skeleton_widget(tab))
            from .atlante import empty_state
            self.cache_stack.addWidget(empty_state(tab, QtCore, QtWidgets, text["atlas_empty_library"]))
            layout.addWidget(self.cache_stack, 1)
            self.cache_more_button = QtWidgets.QPushButton(text["show_more"], tab)
            self.cache_more_button.setObjectName("loadMoreStrip")
            self.cache_more_button.clicked.connect(self._show_more_cache)
            self.cache_more_button.hide()
            layout.addWidget(self.cache_more_button)
            self.cache_table.verticalScrollBar().valueChanged.connect(
                lambda _value: self._load_more_near_bottom("cache")
            )
            actions = QtWidgets.QHBoxLayout()
            self.cache_select_all = QtWidgets.QCheckBox(text["select_all"], tab)
            self.cache_select_all.checkStateChanged.connect(self._set_all_cache_checked)
            delete = QtWidgets.QPushButton(text["delete_selected"], tab)
            delete.clicked.connect(self._delete_selected_cache)
            self.cache_delete_button = delete
            self.cache_delete_button.setEnabled(False)
            clear = QtWidgets.QPushButton(text["clear_cache"], tab)
            clear.clicked.connect(self._clear_cache)
            actions.addWidget(self.cache_select_all)
            actions.addWidget(delete)
            actions.addWidget(clear)
            actions.addStretch()
            layout.addLayout(actions)
            return tab

        def _confirm_library_action(self, message: str, *, icon_name="trash", title=None) -> bool:
            from .shell.messages import compact_message_dialog
            dialog = compact_message_dialog(
                self, QtCore, QtGui, QtWidgets,
                title=title or text["tab_cache"], message=message, dark=self._dark,
                confirm_label=text["guided_fetch_confirm_action"],
                cancel_label=text["cancel_action"], icon_name=icon_name,
                object_name="libraryActionConfirmation",
            )
            try:
                return dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
            finally:
                dialog.deleteLater()

        def _show_library_transfer_notice(self, message: str, *, details=None):
            from .shell.messages import compact_message_dialog
            dialog = compact_message_dialog(
                self, QtCore, QtGui, QtWidgets,
                title=text["tab_cache"], message=message, dark=self._dark,
                confirm_label=text["close"], icon_name="warning",
                details=details, details_label=text["details"],
                object_name="libraryTransferNotice",
            )
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()

        def _choose_library_archive(self, action: str):
            if (
                self._library_busy or self._process is not None
                or self._commands_process is not None or self._library_pool is None
                or library_transfer_callback is None
            ):
                return
            if self._raise_existing_library_dialog():
                return
            if action == "export":
                path, _ = QtWidgets.QFileDialog.getSaveFileName(
                    self, text["library_archive_export"], "Callimachus-library.zip",
                    text["library_archive_filter"],
                )
            else:
                path, _ = QtWidgets.QFileDialog.getOpenFileName(
                    self, text["library_archive_import"], filter=text["library_archive_filter"],
                )
            if not path:
                return
            if action == "import" and not self._confirm_library_action(
                text["library_archive_confirm"], icon_name="library",
                title=text["library_archive_import"],
            ):
                return
            self._library_operation = "archive_" + action
            self._set_library_busy(True)
            self._submit_library_task(
                "archive_" + action, lambda: library_transfer_callback(action, path),
            )

        def _choose_library_documents(self):
            if self._library_busy or library_import_callback is None:
                return
            paths, _selected = QtWidgets.QFileDialog.getOpenFileNames(
                self, text["library_choose_documents"],
                filter=_document_file_filter(text["library_document_filter"]),
            )
            if paths:
                self._start_library_import(paths)

        def _choose_library_folder(self):
            if self._library_busy or library_import_callback is None:
                return
            path = QtWidgets.QFileDialog.getExistingDirectory(
                self, text["library_choose_folder"]
            )
            if path:
                self._start_library_import([path])

        def _start_library_import(self, paths: Sequence[str]):
            if (
                self._library_busy or self._process is not None
                or self._commands_process is not None or self._library_pool is None
                or library_import_callback is None
            ):
                return
            if self._raise_existing_library_dialog():
                return
            self._library_operation = "inspect"
            self._library_import_rows = []
            self._set_library_busy(True)
            self._submit_library_task(
                "import", lambda: library_import_callback(
                    [str(path) for path in paths],
                    lambda event: self._library_signals.progress.emit("import", dict(event)),
                )
            )

        def _review_library_pending(self):
            if (
                self._library_busy or self._process is not None
                or self._commands_process is not None or self._library_pool is None
                or library_pending_loader is None
            ):
                return
            if self._raise_existing_library_dialog():
                return
            self._library_operation = "pending"
            self._set_library_busy(True)
            self._submit_library_task("pending", library_pending_loader)

        def _raise_existing_library_dialog(self):
            dialog = self._library_dialog
            if dialog is None or not dialog.isVisible():
                return False
            dialog.raise_()
            dialog.activateWindow()
            return True

        def _clear_library_dialog(self, dialog):
            if self._library_dialog is dialog:
                self._library_dialog = None

        def _submit_library_task(self, kind: str, operation: Callable[[], Any]):
            recount = kind in {"import", "save", "archive_import"} and library_pending_loader is not None
            if recount or kind == "pending":
                self._library_pending_generation = getattr(self, "_library_pending_generation", 0) + 1
            generation = getattr(self, "_library_pending_generation", 0)
            def work():
                try:
                    result, error = operation(), None
                except Exception as exc:
                    params = getattr(exc, "reason_params", {})
                    result, error = None, {
                        "reason_code": getattr(exc, "reason_code", None),
                        "reason_params": dict(params) if isinstance(params, Mapping) else {},
                    }
                    if kind in {"archive_import", "archive_export"}:
                        import errno
                        cause, seen = exc, set()
                        while cause is not None and id(cause) not in seen:
                            seen.add(id(cause))
                            if (isinstance(cause, PermissionError)
                                    or getattr(cause, "errno", None) in {errno.EACCES, errno.EPERM}
                                    or getattr(cause, "winerror", None) == 5):
                                error = {"reason_code": kind + "_permission_denied", "reason_params": {}}
                                break
                            cause = cause.__cause__ or cause.__context__
                pending_count = None
                if recount:
                    try:
                        pending_count = sum(isinstance(row, Mapping) for row in (library_pending_loader() or ()))
                    except Exception:
                        pass  # The count is unknown; never substitute the selected rows.
                try:
                    if recount:
                        self._library_signals.completed.emit(
                            "pending_count", {"generation": generation, "count": pending_count}, None,
                        )
                    self._library_signals.completed.emit(kind, result, error)
                except RuntimeError:
                    pass
            self._library_pool.submit(work)

        def _library_error_message(self, error: Any) -> str:
            if isinstance(error, Mapping):
                reason_code = error.get("reason_code")
                reason_params = error.get("reason_params")
            else:
                reason_code = getattr(error, "reason_code", None)
                reason_params = getattr(error, "reason_params", None)
            if reason_code and f"library_reason_{reason_code}" in text:
                return library_reason_text(text, reason_code, reason_params)
            return text["library_operation_failed"]

        def _set_library_busy(self, busy: bool):
            self._library_busy = bool(busy)
            self.library_import_button.setEnabled(
                not busy and library_import_callback is not None
            )
            self.library_pending_button.setEnabled(
                not busy and library_pending_loader is not None
            )
            for button in (self.library_archive_import_button, self.library_archive_export_button):
                button.setEnabled(not busy and library_transfer_callback is not None)
            if busy:
                progress_key = {
                    "inspect": "library_inspecting",
                    "pending": "library_loading_pending",
                    "save": "library_saving",
                    "identity": "library_validating_identity",
                    "identity_confirm": "library_confirming_identity",
                    "archive_import": "library_archive_importing",
                    "archive_export": "library_archive_exporting",
                }.get(getattr(self, "_library_operation", ""), "library_working")
                self.library_progress.setText(text[progress_key])
                self.library_progress_bar.setRange(0, 0)
                self.library_progress_bar.show()
            else:
                self.library_progress.setText("")
                self.library_progress_bar.hide()
            if self._library_dialog is not None:
                self._library_dialog.set_busy(busy)
                if busy:
                    self._library_dialog.set_message(text[progress_key])

        def _library_progress_updated(self, kind: str, event: Mapping[str, Any]):
            if not self._library_busy or (kind == "import") != (self._library_operation == "inspect"):
                return
            stage = str(event.get("stage") or "")
            total = int(event.get("total") or 0)
            completed = int(event.get("completed") or 0)
            self.library_progress_bar.setRange(0, total)
            self.library_progress_bar.setValue(completed)
            activity = text.get(f"library_activity_{stage}", text["library_working"])
            filename = Path(str(event.get("file") or "")).name
            message = text["library_current_file"].format(activity=activity, file=filename) if filename else activity
            self.library_progress.setText(message)
            if kind == "save" and self._library_dialog is not None:
                self._library_dialog.set_progress(completed, total, message)
            if kind != "import":
                return
            if stage == "queued":
                self._library_import_rows = [
                    {"file": str(path), "stage": "queued"} for path in event.get("files", ())
                ]
                self._cache_visible_count = self._page_batch_size(self.cache_table)
                self.cache_stack.setCurrentIndex(0 if self.cache_table.rowCount() else 2)
                self._render_cache()
                self.cache_table.scrollToTop()
            elif stage not in {"scanning", "finalizing"}:
                index = int(event.get("index", -1))
                if 0 <= index < len(self._library_import_rows):
                    self._library_import_rows[index]["stage"] = stage
                    item = self.cache_table.item(index, 2)
                    if item is not None:
                        item.setText(activity)
                        item.setToolTip(activity)
                    if stage == "processed":
                        widget = self.cache_table.cellWidget(index, 3)
                        if widget is not None:
                            effect = widget.graphicsEffect()
                            if effect in self._library_skeleton_effects:
                                self._library_skeleton_effects.remove(effect)
                            self.cache_table.removeCellWidget(index, 3)

        def _library_completed(self, kind: str, result: Any, error: Any):
            if kind == "pending_count":
                if (isinstance(result, Mapping)
                        and result.get("generation") == getattr(self, "_library_pending_generation", 0)):
                    from .atlas_collections import set_pending_count
                    set_pending_count(self, result.get("count"))
                return  # A badge refresh must not finish another active operation.
            self._set_library_busy(False)
            if kind in {"archive_import", "archive_export"}:
                if error:
                    code = error.get("reason_code") if isinstance(error, Mapping) else None
                    message = (
                        text["library_reason_" + code]
                        if code in {"archive_import_permission_denied", "archive_export_permission_denied"}
                        else text["library_archive_failed"]
                    )
                    self.library_progress.setText(message)
                    self._show_library_transfer_notice(message)
                    return
                summary = result if isinstance(result, Mapping) else {}
                if kind == "archive_import":
                    message = text["library_archive_import_done"].format(
                        originals=summary.get("originals", 0), texts=summary.get("texts", 0),
                        skipped=summary.get("skipped", 0),
                    )
                else:
                    webtexts = summary.get("webtexts", 0)
                    web_suffix = text["library_archive_export_web_suffix"].format(webtexts=webtexts) if webtexts else ""
                    message = text["library_archive_export_done"].format(
                        fulltexts=summary.get("fulltexts", 0), abstracts=summary.get("abstracts", 0),
                        web_suffix=web_suffix,
                    )
                if kind == "archive_import":
                    self._refresh_cache_after_mutation()
                issues = [dict(issue) for issue in summary.get("issues", ()) if isinstance(issue, Mapping)]
                if issues:
                    message += "\n" + text["library_archive_issues"].format(count=len(issues))
                self.library_progress.setText(message)
                if issues:
                    details = []
                    for issue in issues[:50]:
                        name = str(issue.get("name") or "?").replace("\n", " ").replace("\r", " ")[:300]
                        reason = text.get("library_archive_issue_" + str(issue.get("reason_code") or ""), text["library_archive_issue_other"])
                        details.append(f"{name}: {reason}")
                    if len(issues) > 50:
                        details.append(text["library_archive_issues_more"].format(count=len(issues) - 50))
                    self._show_library_transfer_notice(message, details="\n".join(details))
                return
            if kind == "import":
                self._library_import_rows = []
                self._render_cache()
            if kind == "identity":
                if self._library_dialog is not None:
                    self._library_dialog.apply_identity_result(
                        result if isinstance(result, Mapping) else None,
                        self._library_error_message(error) if error else None,
                    )
                return
            if kind in {"import", "pending"}:
                if error:
                    self.library_progress.setText(self._library_error_message(error))
                    QtWidgets.QMessageBox.warning(
                        self, text["tab_cache"], self._library_error_message(error)
                    )
                    return
                rows = [dict(row) for row in (result or ()) if isinstance(row, Mapping)]
                if kind == "pending":
                    from .atlas_collections import set_pending_count
                    set_pending_count(self, len(rows))
                if not rows:
                    empty_key = "library_no_pending" if kind == "pending" else "library_no_documents"
                    self.library_progress.setText(text[empty_key])
                    return
                self._library_dialog = create_library_review_dialog(
                    QtCore, QtWidgets, text, rows, self
                )
                self._library_dialog.set_dark_theme(self._dark)
                self._library_dialog.set_save_enabled(library_save_callback is not None)
                self._library_dialog.set_identity_enabled(library_identity_callback is not None)
                self._library_dialog.save_requested.connect(self._save_library_rows)
                self._library_dialog.identity_requested.connect(self._review_library_identity)
                self._library_dialog.file_open_requested.connect(self._open_library_review_file)
                dialog = self._library_dialog
                dialog.finished.connect(
                    lambda _result, dialog=dialog: self._clear_library_dialog(dialog)
                )
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                return
            if error:
                message = text["library_save_error"].format(
                    reason=self._library_error_message(error)
                )
                self.library_progress.setText(message)
                if self._library_dialog is not None:
                    self._library_dialog.set_message(message)
                return
            outcomes = [dict(row) for row in (result or ()) if isinstance(row, Mapping)]
            if not outcomes:
                self.library_progress.setText(text["library_save_no_results"])
                return
            if self._library_dialog is not None:
                self._library_dialog.apply_saved(outcomes)
            stored_count = sum(
                str(row.get("status") or "").casefold() in {"stored", "saved"}
                for row in outcomes
            )
            failed_count = len(outcomes) - stored_count
            message = text["library_save_done"].format(count=stored_count, failed=failed_count)
            self.library_progress.setText(message)
            if self._library_dialog is not None:
                self._library_dialog.set_message(message)
            if stored_count:
                if self._background_loading:
                    self._invalidate_load("cache")
                    if self._page_index == 2:
                        self._queue_load("cache", cache_loader)
                else:
                    try:
                        self._apply_cache_rows(cache_loader() or ())
                    except Exception as exc:
                        self.library_progress.setText(
                            text["library_refresh_error"].format(reason=self._library_error_message(exc))
                        )

        def _open_library_review_file(self, path: str):
            try:
                if open_path_callback(path) is not False:
                    return
                reason = text["open_path_unavailable"]
            except (OSError, RuntimeError, ValueError) as exc:
                reason = str(exc)
            QtWidgets.QMessageBox.warning(
                self._library_dialog or self, text["library_open_file"],
                text["library_open_file_failed"].format(path=path, reason=reason),
            )

        def _review_library_identity(self, row: Mapping[str, Any], scheme: str, identifier: str):
            if (
                self._library_busy or self._process is not None
                or self._commands_process is not None or self._library_pool is None
                or library_identity_callback is None
            ):
                return
            self._library_operation = "identity_confirm" if row.get("_operator_attested") is True else "identity"
            self._set_library_busy(True)
            self._submit_library_task(
                "identity", lambda: library_identity_callback(dict(row), scheme, identifier)
            )

        def _save_library_rows(self, rows: Sequence[Mapping[str, Any]]):
            if (
                self._library_busy or self._process is not None
                or self._commands_process is not None or self._library_pool is None
                or library_save_callback is None
            ):
                return
            self._library_pending_rows = [dict(row) for row in rows]
            pending_rows = self._library_pending_rows
            if not pending_rows:
                return
            if self._library_dialog is not None:
                self._library_dialog.accept()
            self._library_operation = "save"
            self._set_library_busy(True)
            self._submit_library_task(
                "save", lambda: library_save_callback(
                    pending_rows,
                    lambda event: self._library_signals.progress.emit("save", dict(event)),
                )
            )

        def _settings_tab(self):
            tab = QtWidgets.QWidget()
            root = QtWidgets.QVBoxLayout(tab)
            root.setContentsMargins(30, 26, 30, 0)
            root.setSpacing(18)
            heading = QtWidgets.QLabel(text["tab_settings"], tab)
            heading.setObjectName("pageTitle")
            self.settings_navigation_toggle = QtWidgets.QPushButton(tab)
            self.settings_navigation_toggle.setObjectName("settingsNavigationToggle")
            self.settings_navigation_toggle.setText(text.get(
                "settings_categories_label",
                "Categories" if self._language == "en" else "Categorie",
            ))
            self.settings_navigation_toggle.setProperty("atlasIcon", "context-settings")
            self.settings_navigation_toggle.setProperty("atlasIconSize", 28)
            from .atlas_icons import icon as atlas_icon
            self.settings_navigation_toggle.setIcon(atlas_icon(
                QtCore, QtGui, "context-settings", size=28,
            ))
            self.settings_navigation_toggle.setIconSize(QtCore.QSize(28, 24))
            self.settings_navigation_toggle.setMinimumSize(0, 0)
            self.settings_navigation_toggle.setMaximumWidth(320)
            self.settings_navigation_toggle.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            self.settings_navigation_toggle.setAccessibleName(text["settings_navigation_toggle"])
            self.settings_navigation_toggle.setToolTip(text["settings_navigation_toggle"])
            root.addWidget(heading)
            description_row = QtWidgets.QWidget(tab)
            description_layout = QtWidgets.QHBoxLayout(description_row)
            self._settings_description_row = description_row
            self._settings_description_layout = description_layout
            self._settings_header_compact = None
            description_layout.setContentsMargins(0, 0, 0, 0)
            description_layout.setSpacing(20)
            summary = QtWidgets.QLabel(text.get("atlas_settings_summary", "Application preferences and source services. Configure models from the Jury chooser."), description_row)
            summary.setObjectName("settingsDescription")
            summary.setWordWrap(True)
            self.settings_description = summary
            summary.installEventFilter(self)
            description_layout.addWidget(summary, 1, QtCore.Qt.AlignmentFlag.AlignTop)
            search_panel = QtWidgets.QWidget(description_row)
            self._settings_search_panel = search_panel
            search_panel.setMinimumWidth(240)
            search_panel.setMaximumWidth(360)
            search_layout = QtWidgets.QVBoxLayout(search_panel)
            search_layout.setContentsMargins(0, 0, 0, 0)
            search_layout.setSpacing(0)
            description_layout.addWidget(
                search_panel, 1, QtCore.Qt.AlignmentFlag.AlignTop
            )
            root.addWidget(description_row)
            self.settings_search = QtWidgets.QLineEdit(search_panel)
            self.settings_search.setObjectName("settingsSearch")
            self.settings_search.setAccessibleName(text["settings_search_label"])
            self.settings_search.setPlaceholderText(text["settings_search_placeholder"])
            self.settings_search.setClearButtonEnabled(True)
            self.settings_search.textChanged.connect(self._refresh_settings_search_results)
            self.settings_search.installEventFilter(self)
            search_layout.addWidget(self.settings_search)
            self.settings_search_results = QtWidgets.QListWidget(tab)
            self.settings_search_results.setObjectName("settingsSearchResults")
            self.settings_search_results.setAccessibleName(text["settings_search_label"])
            self.settings_search_results.setWordWrap(True)
            self.settings_search_results.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
            self.settings_search_results.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.settings_search_results.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
            )
            self.settings_search_results.setMaximumHeight(176)
            self.settings_search_results.hide()
            self._settings_search_size_update_pending = False
            self.settings_search_results.installEventFilter(self)
            self._settings_page = tab
            tab.installEventFilter(self)
            self.pages.currentChanged.connect(self._settings_search_page_changed)
            self.settings_search_results.itemClicked.connect(
                self._activate_settings_search_item
            )
            self.settings_search_results.itemActivated.connect(
                self._activate_settings_search_item
            )
            self.settings_search.returnPressed.connect(
                self._activate_first_settings_search_result
            )
            splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, tab)
            splitter.setObjectName("settingsSidebarSplitter")
            splitter.setChildrenCollapsible(False)
            splitter.setHandleWidth(5)
            self.settings_categories = QtWidgets.QListWidget(tab)
            self.settings_categories.setObjectName("settingsCategories")
            self.settings_categories.setMinimumWidth(165)
            self.settings_categories.setMaximumWidth(360)
            self.settings_stack = QtWidgets.QStackedWidget(tab)
            self.settings_categories.currentRowChanged.connect(self._select_settings_category)
            splitter.addWidget(self.settings_categories)
            settings_panel = QtWidgets.QWidget(tab)
            panel_layout = QtWidgets.QVBoxLayout(settings_panel)
            panel_layout.setContentsMargins(0, 0, 0, 0)
            panel_layout.setSpacing(0)
            scroll = QtWidgets.QScrollArea(settings_panel)
            self.settings_scroll = scroll
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._settings_scroll_viewport = scroll.viewport()
            self._settings_scroll_viewport.installEventFilter(self)
            self.settings_content = QtWidgets.QWidget(scroll)
            self.settings_layout = QtWidgets.QVBoxLayout(self.settings_content)
            content_margins = self.settings_layout.contentsMargins()
            self._settings_wide_content_margins = (
                content_margins.left(), content_margins.top(),
                content_margins.right(), content_margins.bottom(),
            )
            self.settings_layout.setSizeConstraint(
                QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize
            )
            self.settings_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            scroll.setWidget(self.settings_content)
            scroll.verticalScrollBar().rangeChanged.connect(
                self._schedule_settings_search_reveal
            )
            scroll.verticalScrollBar().valueChanged.connect(
                self._settings_search_scroll_value_changed
            )
            panel_layout.addWidget(scroll, 1)
            footer = QtWidgets.QFrame(settings_panel)
            footer.setObjectName("settingsFooter")
            footer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            footer_layout = QtWidgets.QHBoxLayout(footer)
            footer_layout.setContentsMargins(8, 8, 8, 8)
            self.settings_discard_button = QtWidgets.QPushButton(text["discard_changes"], footer)
            self.settings_discard_button.clicked.connect(self._discard_settings)
            self.settings_save_button = QtWidgets.QPushButton(text["save_changes"], footer)
            self.settings_save_button.setObjectName("primaryButton")
            self.settings_save_button.clicked.connect(self._save_settings)
            footer_buttons = (self.settings_discard_button, self.settings_save_button)
            self._settings_footer = footer
            self._settings_footer_buttons = tuple(
                (button, str(button.text())) for button in footer_buttons
            )
            for button, label in self._settings_footer_buttons:
                button.setAccessibleName(label)
                button.setMinimumWidth(0)
                button.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Ignored,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
            footer_layout.addStretch(1)
            footer_layout.addWidget(self.settings_discard_button)
            footer_layout.addWidget(self.settings_save_button)
            footer.installEventFilter(self)
            panel_layout.addWidget(footer)
            self.settings_stack.addWidget(settings_panel)
            self.settings_stack.addWidget(self._skeleton_widget(tab))
            splitter.addWidget(self.settings_stack)
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setSizes([210, 900])
            self.settings_splitter = splitter
            root.addWidget(self.settings_navigation_toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
            from core.gui.shell.navigation import install_sidebar_toggle
            self._settings_navigation_controller = install_sidebar_toggle(
                splitter, self.settings_categories, self.settings_navigation_toggle,
                QtCore, QtWidgets, minimum_content_width=640, preferred_width=210,
                compact_only=True, content_height=True,
            )
            self.settings_categories.itemClicked.connect(
                lambda _item: self._settings_navigation_controller.close_overlay()
            )
            root.addWidget(splitter, 1)
            self._update_settings_footer_labels()
            from core.gui.shell.scroll import install_edge_scrollbar
            self._settings_edge_scrollbar = install_edge_scrollbar(
                tab, [scroll], QtCore, QtWidgets,
            )
            return tab

        def _info_tab(self):
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            layout.setContentsMargins(30, 26, 30, 0)
            title = QtWidgets.QLabel(text["info_title"], tab)
            title.setObjectName("sectionTitle")
            layout.addWidget(title)
            desktop_window = self

            class DocumentationActions(QtWidgets.QWidget):
                def minimumSizeHint(self):
                    return QtCore.QSize(0, super().minimumSizeHint().height())

                def _natural_width(self, control):
                    return min(control.maximumWidth(), control.sizeHint().width())

                def _place_guide_toggle_in_actions(self, in_actions):
                    guide = getattr(desktop_window, "_atlas_guide", None)
                    if guide is None:
                        return
                    toggle = guide._navigation_toggle
                    row = self.layout()
                    guide_layout = guide._primary_view.layout()
                    if in_actions and toggle.parentWidget() is not self:
                        guide_layout.removeWidget(toggle)
                        toggle.setParent(self)
                        row.insertWidget(0, toggle)
                        toggle.show()
                        guide._navigation_controller.sync()
                    elif not in_actions and toggle.parentWidget() is self:
                        row.removeWidget(toggle)
                        toggle.setParent(guide._primary_view)
                        guide_layout.insertWidget(
                            0, toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft
                        )
                        toggle.show()
                        guide._navigation_controller.sync()

                def reflow(self):
                    row = self.layout()
                    guide = getattr(desktop_window, "_atlas_guide", None)
                    toggle = guide._navigation_toggle if guide is not None else None
                    if toggle is not None and toggle.parentWidget() is self:
                        full_width = sum(
                            self._natural_width(control)
                            for control in (toggle, desktop_window.privacy_button,
                                            desktop_window.ai_report_button)
                        ) + 2 * row.spacing()
                        actions_width = sum(
                            self._natural_width(control)
                            for control in (desktop_window.privacy_button,
                                            desktop_window.ai_report_button)
                        ) + row.spacing()
                        if full_width > self.width() and actions_width <= self.width():
                            self._place_guide_toggle_in_actions(False)
                    elif toggle is not None:
                        full_width = sum(
                            self._natural_width(control)
                            for control in (toggle, desktop_window.privacy_button,
                                            desktop_window.ai_report_button)
                        ) + 2 * row.spacing()
                        if full_width <= self.width():
                            self._place_guide_toggle_in_actions(True)

                    controls = [
                        row.itemAt(index).widget()
                        for index in range(row.count())
                        if row.itemAt(index).widget() is not None
                        and not row.itemAt(index).widget().isHidden()
                    ]
                    needed = sum(self._natural_width(control) for control in controls)
                    needed += row.spacing() * max(0, len(controls) - 1)
                    row.setDirection(QtWidgets.QBoxLayout.Direction.LeftToRight
                                     if self.width() >= needed
                                     else QtWidgets.QBoxLayout.Direction.TopToBottom)

                def resizeEvent(self, event):
                    super().resizeEvent(event)
                    self.reflow()

            actions = DocumentationActions(tab)
            self._documentation_actions = actions
            actions.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                  QtWidgets.QSizePolicy.Policy.Preferred)
            privacy_controls = QtWidgets.QHBoxLayout(actions)
            privacy_controls.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
            privacy_controls.setContentsMargins(0, 0, 0, 0)
            privacy_controls.setSpacing(4)
            self.privacy_button = QtWidgets.QPushButton(text["privacy_policy"], tab)
            self.privacy_button.clicked.connect(self._show_privacy_policy)
            self.ai_report_button = QtWidgets.QPushButton(text["report_ai_content"], tab)
            self.ai_report_button.setToolTip(text["report_ai_content_tip"])
            self.ai_report_button.clicked.connect(self._report_ai_content)
            privacy_controls.addWidget(self.privacy_button)
            privacy_controls.addWidget(self.ai_report_button)
            privacy_controls.addStretch()
            layout.addWidget(actions)
            content = QtWidgets.QHBoxLayout()
            content.setContentsMargins(0, 0, 0, 0)
            split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, tab)
            self.docs_outline = QtWidgets.QTreeWidget(tab)
            self.docs_outline.setObjectName("documentationOutline")
            self.docs_outline.setHeaderHidden(True)
            self.docs_outline.setRootIsDecorated(False)
            self.docs_outline.setIndentation(16)
            self.docs_outline.setUniformRowHeights(False)
            self.docs_outline.setWordWrap(True)
            self.docs_outline.setMinimumWidth(260)
            self.docs_outline.setMaximumWidth(380)
            self.docs_view = QtWidgets.QTextBrowser(tab)
            self.docs_view.setReadOnly(True)
            self.docs_view.setOpenLinks(False)
            self.docs_outline.currentItemChanged.connect(self._navigate_documentation)
            self.docs_view.anchorClicked.connect(self._open_doc_link)
            split.addWidget(self.docs_outline)
            self.docs_stack = QtWidgets.QStackedWidget(tab)
            self.docs_stack.addWidget(self.docs_view)
            self.docs_stack.addWidget(self._skeleton_widget(tab))
            split.addWidget(self.docs_stack)
            split.setStretchFactor(0, 0)
            split.setStretchFactor(1, 1)
            split.setSizes([300, 600])
            content.addWidget(split)
            reference = QtWidgets.QWidget(tab)
            reference.setLayout(content)
            from .desktop_guide import create_desktop_guide
            self._atlas_guide = create_desktop_guide(
                QtCore, QtGui, QtWidgets,
                parent=tab,
                language=self._language,
                reference_widget=reference,
                privacy_callback=self._show_privacy_policy,
            )
            # Preserve the reused button while invalidating its old layout item.
            self._atlas_guide._primary_view.layout().removeWidget(
                self._atlas_guide._navigation_toggle
            )
            privacy_controls.insertWidget(0, self._atlas_guide._navigation_toggle)
            actions.reflow()
            layout.addWidget(self._atlas_guide, 1)
            from core.gui.shell.scroll import install_edge_scrollbar
            self._atlas_guide._edge_scrollbar = install_edge_scrollbar(
                tab, [self._atlas_guide._home_scroll,
                      self._atlas_guide._directory_scroll,
                      self._atlas_guide._article_scroll, self.docs_view],
                QtCore, QtWidgets,
            )
            return tab

        def _commands_tab(self):
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            layout.setContentsMargins(30, 26, 30, 0)
            title = QtWidgets.QLabel(text["commands_title"], tab)
            title.setObjectName("sectionTitle")
            layout.addWidget(title)

            self.commands_expert_notice = QtWidgets.QLabel(
                text["commands_expert_notice"], tab
            )
            self.commands_expert_notice.setObjectName("commandsExpertNotice")
            self.commands_expert_notice.setWordWrap(True)
            console_intro = QtWidgets.QHBoxLayout()
            console_intro.addWidget(self.commands_expert_notice, 1)
            layout.addLayout(console_intro)

            console_tools = QtWidgets.QHBoxLayout()
            self.console_history_spinner = QtWidgets.QLabel("◐", tab)
            self.console_history_spinner.setObjectName("consoleHistorySpinner")
            self.console_history_spinner.setAccessibleName(
                text["commands_history_loading"]
            )
            self.console_history_spinner.hide()
            console_tools.addWidget(self.console_history_spinner)
            self.commands_history_button = QtWidgets.QPushButton(
                text["commands_load_earlier"], tab
            )
            self.commands_history_button.setObjectName("commandsHistoryButton")
            self.commands_history_button.clicked.connect(
                self._load_older_console_page
            )
            self.commands_history_button.hide()
            console_tools.addWidget(self.commands_history_button)
            self.commands_gap_button = QtWidgets.QPushButton(
                text["commands_load_missing"], tab
            )
            self.commands_gap_button.setObjectName("commandsGapButton")
            self.commands_gap_button.clicked.connect(self._load_console_gap_page)
            self.commands_gap_button.hide()
            console_tools.addWidget(self.commands_gap_button)
            console_tools.addStretch(1)
            self.commands_export_button = QtWidgets.QPushButton(
                text["commands_export"], tab
            )
            self.commands_export_button.setObjectName("commandsExportButton")
            self.commands_export_button.setEnabled(False)
            self.commands_export_button.clicked.connect(self._choose_console_export)
            console_intro.addWidget(
                self.commands_export_button, 0,
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop,
            )
            layout.addLayout(console_tools)

            self.console_stack = QtWidgets.QStackedWidget(tab)
            self.commands_output = QtWidgets.QPlainTextEdit(tab)
            self.commands_output.setObjectName("commandsOutput")
            self.commands_output.setReadOnly(True)
            output_font = QtGui.QFontDatabase.systemFont(
                QtGui.QFontDatabase.SystemFont.FixedFont
            )
            output_font.setPixelSize(max(15, self._font_size + 2))
            self.commands_output.setFont(output_font)
            self.commands_output.setUndoRedoEnabled(False)
            self.commands_output.verticalScrollBar().valueChanged.connect(
                self._on_console_scroll_changed
            )
            self.console_stack.addWidget(self.commands_output)
            self.console_stack.addWidget(self._skeleton_widget(tab))
            self.console_stack.setCurrentWidget(self.commands_output)
            layout.addWidget(self.console_stack, 1)

            self.commands_status_label = QtWidgets.QLabel("", tab)
            self.commands_status_label.setWordWrap(True)
            self.commands_status_label.hide()
            layout.addWidget(self.commands_status_label)

            self._commands_displayed_prefix = _commands_displayed_prefix()
            self.commands_input = QtWidgets.QLineEdit(tab)
            self.commands_run_button = QtWidgets.QPushButton(
                text["commands_run"], tab
            )
            self.commands_run_button.clicked.connect(self._run_commands)
            self.commands_input.returnPressed.connect(self.commands_run_button.click)
            prompt = QtWidgets.QHBoxLayout()
            prompt.addWidget(self.commands_input, 1)
            prompt.addWidget(self.commands_run_button)
            layout.addLayout(prompt)
            return tab

        @staticmethod
        def _table(parent, labels):
            table = QtWidgets.QTableWidget(0, len(labels), parent)
            table.setHorizontalHeaderLabels(labels)
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setStretchLastSection(True)
            return table

        def _skeleton_widget(self, parent):
            class _AnimatedSkeleton(QtWidgets.QWidget):
                def __init__(self, owner):
                    super().__init__(owner)
                    self._atlas_loading_animations = []

                def showEvent(self, event):
                    super().showEvent(event)
                    for animation in self._atlas_loading_animations:
                        animation.start()

                def hideEvent(self, event):
                    for animation in self._atlas_loading_animations:
                        animation.stop()
                    super().hideEvent(event)

            from .atlas_loading import make_loading_bar

            widget = _AnimatedSkeleton(parent)
            layout = QtWidgets.QVBoxLayout(widget)
            layout.setContentsMargins(18, 18, 18, 18)
            layout.setSpacing(13)
            for index in range(7):
                bar, animation = make_loading_bar(
                    QtCore, QtGui, QtWidgets, widget,
                    lambda: self._dark,
                    duration=900 + index * 80,
                )
                bar.setObjectName("loadingSkeletonBar")
                bar.setFixedHeight(20 if index % 3 == 0 else 14)
                bar.setMaximumWidth(560 if index % 3 == 0 else 350 + index * 18)
                widget._atlas_loading_animations.append(animation)
                self._skeleton_animations.append(animation)
                layout.addWidget(bar)
            layout.addStretch()
            return widget

        @staticmethod
        def _page_batch_size(table):
            row_height = max(20, table.verticalHeader().defaultSectionSize())
            visible_rows = max(1, table.viewport().height() // row_height)
            return max(12, min(80, visible_rows * 2))

        def _reset_source_page(self, *_args):
            self._cancel_source_detail_next()
            self._source_visible_count = self._page_batch_size(self.source_table)
            if self._source_page_mode:
                self._request_source_query()
            else:
                self._render_sources()

        def _reset_cache_page(self, *_args):
            if cache_page_loader is not None:
                self._invalidate_load("cache_page")
                self._cache_rows = []
                self._cache_visible_count = 0
                self._atlas_cache_total = 0
                self._request_inventory_page("cache")
                return
            self._cache_visible_count = self._page_batch_size(self.cache_table)
            self._render_cache()

        def _refresh_cache_after_mutation(self):
            if self._background_loading and cache_page_loader is not None:
                self._reset_cache_page()
            self._refresh_static_tabs()

        def _reset_history_page(self):
            self._invalidate_load("history_page")
            self._atlas_history_total = 0
            self._history_rows = []
            self._history_visible_count = 0
            self._history_requested_dirs.clear()
            self._history_checked_run_dirs.clear()
            self.history_table.setRowCount(0)
            self.history_more_button.hide()
            self.history_more_button.setEnabled(True)

        def _refresh_history_after_mutation(self):
            if self._background_loading and history_page_loader is not None:
                self._reset_history_page()
            self._refresh_static_tabs()

        def _show_more_sources(self):
            if self._source_page_mode and self._source_query_active():
                self._load_source_query(append=True)
                return
            if self._source_page_mode and not self._source_query_active():
                self._request_source_page()
                return
            self._source_visible_count += self._page_batch_size(self.source_table)
            self._render_sources()

        def _source_query_active(self) -> bool:
            return bool(
                self.source_search.text().strip()
                or self.source_filter.currentText() != text["all"]
                or self.source_phase_filter.currentData()
                or self._source_sort_column is not None
                or bool(getattr(self, "_atlas_filter_keys", ()))
            )

        def _source_query_context(self):
            filters = set(getattr(self, "_atlas_filter_keys", ()))
            legacy = {
                text["fulltext"]: "fulltext",
                text["abstract"]: "abstract",
                text["no_text"]: "none",
            }.get(self.source_filter.currentText())
            if legacy:
                filters.add(legacy)
            return (
                self.source_search.text(),
                tuple(sorted(filters)),
                self.source_phase_filter.currentData(),
                self._source_sort_column,
                self._source_sort_ascending,
            )

        def _request_source_page(self):
            if (
                not self._source_page_mode or not self._run_dir
                or self._page_index != 0 or self._source_query_active()
                or self._child_database_bootstrap_pending(self._run_dir)
                or self._child_source_page_waiting_for_overview(self._run_dir)
                or "source_page" in self._loading_kinds
                or (self._source_page_loaded_run == self._run_dir
                    and self._source_next_offset >= self._source_paged_total)
            ):
                return
            run_dir = self._run_dir
            offset = self._source_next_offset
            limit = self._page_batch_size(self.source_table)
            if offset == 0:
                self.source_stack.setCurrentIndex(1)
            self.source_more_button.setEnabled(False)
            self._queue_load(
                "source_page",
                lambda: (run_dir, offset, source_page_loader(run_dir, offset, limit)),
            )

        def _child_database_bootstrap_pending(self, run_dir: str | None) -> bool:
            if (
                not self._source_page_mode
                or not run_dir
                or self._pending_child_source_dir != run_dir
            ):
                return False
            return not Path(run_dir, "run.sqlite").is_file()

        def _child_source_page_waiting_for_overview(self, run_dir: str | None) -> bool:
            return bool(
                self._source_page_mode
                and run_dir
                and self._pending_child_source_dir == run_dir
                and self._snapshot_loaded_run != run_dir
            )

        def _release_child_database_bootstrap(self, *, refresh: bool = True) -> bool:
            run_dir = self._run_dir
            if (
                not self._source_page_mode
                or not run_dir
                or self._pending_child_source_dir != run_dir
            ):
                return False
            self._pending_child_source_dir = None
            if refresh:
                if self._page_index == 0:
                    if self._source_query_active():
                        self._request_source_query()
                    else:
                        self._request_source_page()
                    self._request_snapshot(force=True)
            return True

        def _refresh_source_pages(self):
            if (
                not self._source_page_mode or not self._run_dir
                or self._page_index != 0 or self._source_query_active()
                or self._child_database_bootstrap_pending(self._run_dir)
                or self._child_source_page_waiting_for_overview(self._run_dir)
                or self._source_page_loaded_run != self._run_dir
                or "source_page" in self._loading_kinds
                or "source_refresh" in self._loading_kinds
            ):
                return
            run_dir = self._run_dir
            signature = self._source_overview_signature
            limit = max(
                self._page_batch_size(self.source_table), len(self._source_paged_rows)
            )
            self._queue_load(
                "source_refresh",
                lambda: (run_dir, signature, source_page_loader(run_dir, 0, limit)),
            )

        def _request_source_query(self):
            if (
                not self._source_page_mode
                or self._child_database_bootstrap_pending(self._run_dir)
                or self._child_source_page_waiting_for_overview(self._run_dir)
            ):
                return
            self._source_query_timer.stop()
            self._atlas_source_cancel.set()
            self._invalidate_load("source_query")
            self._invalidate_load("source_query_refresh")
            self._source_query_refresh_failed = False
            self._source_query_refresh_pending = False
            if not self._source_query_active():
                self._source_query_signature = None
                self._source_rows = list(self._source_paged_rows)
                self._source_visible_count = max(
                    self._page_batch_size(self.source_table), len(self._source_rows)
                )
                self._render_sources()
                if self._source_page_loaded_run == self._run_dir:
                    self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
                    if self._source_page_stale:
                        self._refresh_source_pages()
                else:
                    self._request_source_page()
                return
            if self._run_dir and self._page_index == 0:
                self.source_stack.setCurrentIndex(1)
                self._source_query_timer.start(250)

        def _request_source_query_refresh(self):
            if (
                not self._source_page_mode or not self._run_dir
                or self._page_index != 0 or not self._source_query_active()
                or self._child_database_bootstrap_pending(self._run_dir)
                or self._child_source_page_waiting_for_overview(self._run_dir)
                or self._source_query_timer.isActive()
                or "source_query" in self._loading_kinds
                or "source_query_refresh" in self._loading_kinds
            ):
                return
            context = self._source_query_context()
            if context != self._source_query_signature:
                return

            from .atlante import filtered_source_page
            run_dir = self._run_dir
            overview_signature = self._source_overview_signature
            query, filter_keys, phase, column, ascending = context
            filters = set(filter_keys)
            limit = max(self._page_batch_size(self.source_table), len(self._source_rows))
            self._atlas_source_cancel = threading.Event()
            cancellation = self._atlas_source_cancel
            self._source_query_refresh_pending = False
            self._queue_load(
                "source_query_refresh",
                lambda: (
                    run_dir,
                    context,
                    overview_signature,
                    filtered_source_page(
                        lambda start, size: source_page_loader(run_dir, start, size),
                        query=query,
                        filters=filters,
                        phase=phase,
                        offset=0,
                        limit=limit,
                        sort_column=column,
                        ascending=ascending,
                        cancelled=cancellation,
                    ),
                ),
            )
            self._source_query_refresh_failed = False

        def _load_source_query(self, append=False):
            if (
                not self._run_dir or self._page_index != 0
                or not self._source_query_active()
                or self._child_database_bootstrap_pending(self._run_dir)
                or self._child_source_page_waiting_for_overview(self._run_dir)
            ):
                return
            if "source_query" in self._loading_kinds:
                return
            if append and "source_query_refresh" in self._loading_kinds:
                self._source_query_refresh_pending = True
            self._invalidate_load("source_query_refresh")
            from .atlante import filtered_source_page
            run_dir = self._run_dir
            filters = set(getattr(self, "_atlas_filter_keys", ()))
            legacy = {text["fulltext"]: "fulltext", text["abstract"]: "abstract",
                      text["no_text"]: "none"}.get(self.source_filter.currentText())
            if legacy:
                filters.add(legacy)
            query = self.source_search.text()
            phase = self.source_phase_filter.currentData()
            column, ascending = self._source_sort_column, self._source_sort_ascending
            offset = len(self._source_rows) if append else 0
            signature = (query, tuple(sorted(filters)), phase, column, ascending)
            self._source_query_signature = signature
            overview_signature = self._source_overview_signature
            if not append:
                self._source_rows = []
            self._atlas_source_cancel.set()
            self._atlas_source_cancel = threading.Event()
            cancellation = self._atlas_source_cancel
            limit = self._page_batch_size(self.source_table)
            self.source_more_button.setEnabled(False)
            self._queue_load("source_query", lambda: (
                run_dir,
                signature,
                overview_signature,
                filtered_source_page(
                    lambda start, size: source_page_loader(run_dir, start, size),
                    query=query,
                    filters=filters,
                    phase=phase,
                    offset=offset,
                    limit=limit,
                    sort_column=column,
                    ascending=ascending,
                    cancelled=cancellation,
                ),
            ))

        def _show_more_history(self):
            if history_page_loader is not None:
                self._request_inventory_page("history")
                return
            self._history_visible_count += self._page_batch_size(self.history_table)
            self._apply_history_rows(self._history_rows)
            self._queue_history_details()

        def _queue_history_details(self):
            if (
                not self._background_loading
                or history_detail_loader is None
                or self._page_index != 1
                or "history_details" in self._loading_kinds
            ):
                return
            run_dirs = [
                str(row.get("run_dir") or "")
                for row in self._history_rows[:self._history_visible_count]
                if row.get("summary_pending")
                and str(row.get("run_dir") or "") not in self._history_requested_dirs
            ]
            if not run_dirs:
                return
            run_dir = run_dirs[0]
            self._history_requested_dirs.add(run_dir)

            def load_details():
                try:
                    detail = history_detail_loader(run_dir)
                    return {run_dir: {**dict(detail), "summary_pending": False}}
                except Exception as exc:
                    return {run_dir: {
                        "summary_pending": False,
                        "summary_error": f"{type(exc).__name__}: {exc}",
                    }}

            self._queue_load("history_details", load_details)

        def _show_more_cache(self):
            if cache_page_loader is not None:
                self._request_inventory_page("cache")
                return
            self._cache_visible_count += self._page_batch_size(self.cache_table)
            self._render_cache()

        def _request_inventory_page(self, kind):
            index, table, stack, button, rows, total, loader = (
                (1, self.history_table, self.history_stack, self.history_more_button,
                 self._history_rows, self._atlas_history_total, history_page_loader)
                if kind == "history" else
                (2, self.cache_table, self.cache_stack, self.cache_more_button,
                 self._cache_rows, self._atlas_cache_total, cache_page_loader)
            )
            key = kind + "_page"
            if self._page_index != index or loader is None or key in self._loading_kinds:
                return
            offset = len(rows)
            if offset and offset >= total:
                return
            limit = self._page_batch_size(table)
            if not offset and not (kind == "cache" and self._library_import_rows):
                stack.setCurrentIndex(1)
            button.setEnabled(False)
            if kind == "cache":
                query = self.cache_search.text().strip()
                tier = {text["fulltext"]: "fulltext", text["abstract"]: "abstract",
                        text["no_text"]: "none"}.get(self.cache_filter.currentText(), "")
                call = lambda: loader(offset, limit, query=query, tier=tier)
            else:
                call = lambda: loader(offset, limit)
            self._queue_load(key, lambda: (offset, call()))

        def _load_more_near_bottom(self, kind: str):
            table, button, callback = {
                "source": (self.source_table, self.source_more_button, self._show_more_sources),
                "history": (self.history_table, self.history_more_button, self._show_more_history),
                "cache": (self.cache_table, self.cache_more_button, self._show_more_cache),
            }[kind]
            scrollbar = table.verticalScrollBar()
            if button.isVisible() and scrollbar.maximum() > 0 and scrollbar.value() >= scrollbar.maximum() - 2:
                QtCore.QTimer.singleShot(0, callback)

        def _queue_load(self, kind: str, loader: Callable[[], Any]):
            if self._load_closed or self._load_pool is None or kind in self._loading_kinds:
                return
            token = self._load_tokens.get(kind, 0) + 1
            self._load_tokens[kind] = token
            self._loading_kinds.add(kind)
            signals = self._load_signals

            def work():
                try:
                    payload, error = loader(), None
                except Exception as exc:
                    payload, error = None, f"{type(exc).__name__}: {exc}"
                try:
                    signals.completed.emit(kind, token, payload, error)
                except RuntimeError:
                    pass

            self._load_futures[kind] = self._load_pool.submit(work)

        def _stop_source_service_check_watchdog(
            self, kind: str, token: int | None = None,
        ):
            current = self._source_service_check_watchdogs.get(kind)
            if current is None or (token is not None and current[0] != token):
                return
            self._source_service_check_watchdogs.pop(kind, None)
            timer = current[1]
            timer.stop()
            timer.deleteLater()

        def _invalidate_load(self, kind: str):
            if kind.startswith("source_service_check:"):
                self._stop_source_service_check_watchdog(kind)
            self._load_tokens[kind] = self._load_tokens.get(kind, 0) + 1
            self._loading_kinds.discard(kind)
            self._refresh_collection_presentation()
            future = self._load_futures.pop(kind, None)
            if future is not None:
                future.cancel()

        def _request_page_data(self, index: int):
            if not self._background_loading:
                return
            if index == 0:
                if not self._latest_loaded:
                    self._queue_load("latest", latest_loader)
                if self._source_page_mode and self._run_dir:
                    if self._source_query_active():
                        self._request_source_query()
                    elif self._source_page_loaded_run == self._run_dir:
                        self._source_rows = list(self._source_paged_rows)
                        self._render_sources()
                        self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
                        if self._source_page_stale:
                            self._refresh_source_pages()
                    else:
                        self._request_source_page()
                if not self._run_dir or (
                    self._snapshot_loaded_run == self._run_dir
                    and self._process is None
                    and self._pending_finish is None
                ):
                    self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
                else:
                    self._request_snapshot(force=True)
            elif index == 1:
                self.history_stack.setCurrentIndex(1)
                if history_page_loader is not None:
                    self._request_inventory_page("history")
                elif history_brief_loader is not None and history_detail_loader is not None:
                    self._queue_load("history_brief", history_brief_loader)
                else:
                    self._queue_load("history", history_loader)
            elif index == 2:
                self.cache_stack.setCurrentIndex(0 if self._library_import_rows else 1)
                if self._library_import_rows:
                    self._render_cache()
                if cache_page_loader is not None:
                    self._request_inventory_page("cache")
                else:
                    self._queue_load("cache", cache_loader)
            elif index == 3:
                self.settings_stack.setCurrentIndex(1)
                self._queue_load("settings", settings_loader)
            elif index == 4:
                self._load_console_capture()
            elif index == 5:
                self.docs_stack.setCurrentIndex(1)
                self._queue_load("docs", docs_loader)
            if self._pending_finish is not None:
                self._request_snapshot(force=True)

        def _release_page_data(self, index: int):
            if index == 4:
                if self._background_loading:
                    self._invalidate_load("console")
                    self._invalidate_load("console_older")
                    self._invalidate_load("console_gap")
                self._console_loaded = False
                self._console_page_loading = False
                self.console_history_spinner.hide()
                self.commands_history_button.hide()
                self.commands_gap_button.hide()
                self.commands_output.clear()
                self._reset_console_document_state()
                return
            if not self._background_loading:
                return
            kind = {0: "snapshot", 1: "history", 2: "cache", 3: "settings", 5: "docs"}.get(index)
            if kind:
                self._invalidate_load(kind)
            if index == 0:
                self._cancel_source_detail_next()
                self._invalidate_load("latest_resume")
                self._invalidate_load("detail")
                if self._detail_dialog is not None:
                    self._detail_dialog[0].close()
                reset_overlay = getattr(self, "_atlas_reset_source_detail_overlay", None)
                if reset_overlay is not None:
                    reset_overlay()
                self._atlas_source_cancel.set()
                if self._atlas_counts_cancel is not None:
                    self._atlas_counts_cancel.set()
                self._invalidate_load("source_filter_counts")
                self._source_query_timer.stop()
                self._invalidate_load("source_page")
                self._invalidate_load("source_refresh")
                self._invalidate_load("source_query")
                self._invalidate_load("source_query_refresh")
                if not self._latest_loaded:
                    self._invalidate_load("latest")
                if self._source_page_mode:
                    self._source_paged_rows = []
                    self._source_next_offset = 0
                    self._source_paged_total = 0
                    self._source_page_loaded_run = None
                    self._source_rows = []
                    self.source_table.setRowCount(0)
                    self.source_stack.setCurrentIndex(1)
                elif self._snapshot_loaded_run != self._run_dir:
                    self._source_rows = []
                    self._source_visible_count = 0
                    self.source_table.setRowCount(0)
                    self.source_stack.setCurrentIndex(1 if self._run_dir else 0)
                self._snapshot_loading_run = None
            elif index == 1:
                self._invalidate_load("history_page")
                self._atlas_history_total = 0
                self._invalidate_load("history_brief")
                self._invalidate_load("history_details")
                self._history_rows = []
                self._history_visible_count = 0
                self._history_requested_dirs.clear()
                self.history_table.setRowCount(0)
                self.history_more_button.hide()
            elif index == 2:
                self._invalidate_load("cache_page")
                self._atlas_cache_total = 0
                self._cache_rows = []
                self._cache_visible_count = 0
                self._library_skeleton_effects.clear()
                self.cache_table.setRowCount(0)
                self.cache_more_button.hide()
            elif index == 3:
                self._settings_loaded = False
                self._refresh_signing_key_controls()
            elif index == 5:
                self._documents.clear()
                self.docs_outline.clear()
                self.docs_view.clear()

        def _load_completed(self, kind: str, token: int, payload: Any, error: Any):
            if self._load_closed or token != self._load_tokens.get(kind):
                return
            if kind.startswith("source_service_check:"):
                self._stop_source_service_check_watchdog(kind, token)
            self._loading_kinds.discard(kind)
            self._load_futures.pop(kind, None)
            if kind == "report_export":
                self._report_export_completed(payload, error)
                return
            if kind == "settings_inspect":
                self.centralWidget().setEnabled(True)
                self.statusBar().clearMessage()
                path = self._settings_import_path
                self._settings_import_path = None
                if error or not isinstance(payload, Mapping) or not payload.get("ok"):
                    QtWidgets.QMessageBox.warning(self, text["tab_settings"], str(payload.get("error") or text["settings_transfer_failed"]) if isinstance(payload, Mapping) else text["settings_transfer_failed"])
                    return
                from core.gui.settings.transfer import options
                choices = options(self, QtWidgets, text, "import", encrypted=bool(payload["encrypted"]))
                if choices is not None:
                    self._queue_settings_operation("import", {**choices, "path": path})
                else:
                    self.statusBar().clearMessage()
                return
            if kind == "settings_transfer":
                self.centralWidget().setEnabled(True)
                if error or not isinstance(payload, Mapping) or not payload.get("ok"):
                    QtWidgets.QMessageBox.warning(self, text["tab_settings"], str((payload or {}).get("error") if isinstance(payload, Mapping) else "") or text["settings_transfer_failed"])
                    return
                if payload.get("action") in {"import", "reset"}:
                    self._settings_environment_names.update(
                        name for name in payload.get("environment_names", ())
                        if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                    )
                    self._apply_transferred_preferences(payload.get("preferences") or {})
                    QtCore.QSettings("Callimachus", "Callimachus").setValue(
                        "settings_environment_names", sorted(self._settings_environment_names)
                    )
                    self._settings_dirty.clear()
                    self._settings_replacing.clear()
                    self._settings_loaded = False
                    self._settings_transfer_restore_pending = True
                    self._refresh_static_tabs()
                self.statusBar().showMessage(text["settings_transfer_done"] + (" " + text["language_restart"] if payload.get("action") in {"import", "reset"} else ""), 12000)
                return
            if kind == "source_filter_counts":
                if not error and isinstance(payload, tuple):
                    run_dir, counts = payload
                    if run_dir == self._run_dir and isinstance(counts, Mapping):
                        from .atlante import apply_source_counts
                        apply_source_counts(self, counts)
                return
            if kind == "browser_cache":
                self._browser_cache_clearing = False
                ok = not error and isinstance(payload, Mapping) and payload.get("ok") and not payload.get("deferred")
                message = text["browser_cache_cleared"] if ok else text["browser_cache_deferred"]
                self.statusBar().showMessage(message, 10000)
                status = getattr(self, "_browser_cache_status", None)
                try:
                    if status is not None:
                        status.setText(message)
                    self._browser_cache_clear_button.setEnabled(True)
                except (RuntimeError, AttributeError):
                    pass
                if self._browser_cache_close_pending:
                    self._browser_cache_exit_done = True
                    QtCore.QTimer.singleShot(0, self.close)
                return
            if kind in {"console", "console_older", "console_gap"}:
                if self._page_index == 4:
                    if kind in {"console_older", "console_gap"}:
                        self._console_page_loading = False
                        self._console_page_kind = None
                        self.console_history_spinner.hide()
                    if error:
                        self._set_commands_status(
                            text["load_failed"].format(reason=error), error=True
                        )
                        self._console_load_error = True
                        self.console_stack.setCurrentWidget(self.commands_output)
                    elif isinstance(payload, tuple):
                        try:
                            if kind == "console":
                                self._apply_console_snapshot(payload)
                            elif kind == "console_older":
                                self._apply_console_history_page(payload)
                            else:
                                self._apply_console_gap_page(payload)
                        except OSError as exc:
                            self._set_commands_status(
                                text["load_failed"].format(reason=exc), error=True
                            )
                            self._console_load_error = True
                            self.console_stack.setCurrentWidget(self.commands_output)
                    self._update_console_history_controls()
                return
            if kind == "llm_preflight":
                self._apply_llm_preflight(payload, error)
                return
            if kind == "jury_readiness":
                self._complete_jury_readiness(payload, error)
                return
            if kind.startswith("provider_check:"):
                _prefix, _separator, provider_id = kind.partition(":")
                self._apply_provider_check(provider_id, payload, error)
                self._refresh_api_key_actions()
                return
            if kind.startswith("source_service_check:"):
                name = kind.partition(":")[2]
                result = payload if isinstance(payload, Mapping) and not error else {"ok": False, "error": text["provider_check_error"]}
                self._service_check_state[name] = dict(result)
                self._refresh_api_key_actions()
                if not result.get("ok"):
                    self._show_provider_check_warning(str(result.get("error") or text["provider_check_error"]))
                return
            if kind == "snapshot":
                self._snapshot_loading_run = None
                run_dir, snapshot = payload if isinstance(payload, tuple) else (None, None)
                if run_dir != self._run_dir:
                    return
                if error:
                    snapshot = None
                    self._show_load_error("snapshot", str(error))
                elif isinstance(snapshot, Mapping):
                    snapshot = self._apply_snapshot(dict(snapshot))
                    self._snapshot_loaded_run = run_dir
                    if (
                        self._pending_child_source_dir == run_dir
                        and not self._child_database_bootstrap_pending(run_dir)
                    ):
                        self._pending_child_source_dir = None
                    if self._source_page_mode:
                        counts = snapshot.get("progress_counts")
                        source_counts = (
                            tuple(counts.get(key) for key in (
                                "sources_total", "resolve_completed", "fetch_completed",
                                "verify_total", "verify_completed",
                            ))
                            if isinstance(counts, Mapping) else ()
                        )
                        signature = (
                            snapshot.get("phase"), snapshot.get("run_status"),
                            snapshot.get("updated_at"), snapshot.get("phase_progress"),
                            snapshot.get("verification_activity_revision"),
                            source_counts,
                        )
                        previous_signature = self._source_overview_signature
                        self._source_overview_signature = signature
                        if (
                            previous_signature is not None
                            and signature != previous_signature
                        ):
                            self._source_page_stale = True
                            if self._source_query_active() and self._page_index == 0:
                                self._request_source_query_refresh()
                            else:
                                self._refresh_source_pages()
                        if (
                            (
                                self._source_query_refresh_failed
                                or self._source_query_refresh_pending
                            )
                            and self._source_query_active()
                            and self._page_index == 0
                        ):
                            self._request_source_query_refresh()
                        if self._page_index == 0:
                            if self._source_query_active():
                                if self._source_query_signature is None:
                                    self._request_source_query()
                            elif self._source_page_loaded_run != run_dir:
                                self._request_source_page()
                            elif (
                                isinstance(counts, Mapping)
                                and isinstance(counts.get("sources_total"), int)
                                and counts["sources_total"] != self._source_paged_total
                            ):
                                self._source_page_stale = True
                                self._refresh_source_pages()
                    if not self._source_page_mode:
                        self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
                if self._pending_finish is not None:
                    finish = self._pending_finish
                    self._pending_finish = None
                    self._finish_process_after_snapshot(snapshot, *finish)
                return
            if kind == "source_refresh":
                run_dir, signature, page = payload if isinstance(payload, tuple) else (None, None, None)
                if error:
                    self._show_load_error(kind, str(error))
                    return
                if run_dir != self._run_dir or not isinstance(page, Mapping):
                    return
                references = page.get("references")
                total = page.get("total")
                if not isinstance(references, list) or not isinstance(total, int) or total < 0:
                    self._show_load_error(kind, "invalid source refresh")
                    return
                self._source_paged_rows = [
                    row for row in references if isinstance(row, Mapping)
                ]
                if self._pending_child_source_dir == run_dir:
                    self._pending_child_source_dir = None
                self._source_next_offset = len(self._source_paged_rows)
                self._source_paged_total = total
                self._source_page_stale = signature != self._source_overview_signature
                if not self._source_query_active() and self._page_index == 0:
                    self._source_rows = list(self._source_paged_rows)
                    self._source_visible_count = max(
                        self._page_batch_size(self.source_table), len(self._source_rows)
                    )
                    self._render_sources()
                    self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
                if self._source_page_stale:
                    self._refresh_source_pages()
                return
            if kind == "source_page":
                self.source_more_button.setEnabled(True)
                run_dir, offset, page = payload if isinstance(payload, tuple) else (None, None, None)
                if run_dir != self._run_dir or error:
                    if error:
                        self._cancel_source_detail_next()
                        self._show_load_error(kind, str(error))
                    return
                if not isinstance(page, Mapping) or offset != self._source_next_offset:
                    self._cancel_source_detail_next()
                    self._show_load_error(kind, "invalid source page")
                    return
                references = page.get("references") or []
                total = page.get("total")
                if not isinstance(total, int) or total < 0 or not isinstance(references, list):
                    self._cancel_source_detail_next()
                    self._show_load_error(kind, "invalid source page")
                    return
                if self._pending_child_source_dir == run_dir:
                    self._pending_child_source_dir = None
                self._source_paged_rows.extend(
                    row for row in references if isinstance(row, Mapping)
                )
                self._source_next_offset += len(references)
                self._source_paged_total = total
                self._source_page_loaded_run = run_dir
                if not self._source_query_active():
                    self._source_rows = list(self._source_paged_rows)
                    self._source_visible_count = max(
                        self._page_batch_size(self.source_table), len(self._source_rows)
                    )
                    self._render_sources()
                    self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
                    if self._source_page_stale:
                        self._refresh_source_pages()
                self._continue_source_detail_next()
                return
            if kind == "source_query":
                run_dir, signature, overview_signature, snapshot = (
                    payload if isinstance(payload, tuple) and len(payload) == 4
                    else (None, None, None, None)
                )
                if error:
                    self._cancel_source_detail_next()
                    self._show_load_error(kind, str(error))
                    return
                if run_dir != self._run_dir or signature != self._source_query_signature:
                    self._cancel_source_detail_next()
                    return
                if not isinstance(snapshot, Mapping):
                    self._cancel_source_detail_next()
                    self._show_load_error(kind, "invalid source search")
                    return
                if self._pending_child_source_dir == run_dir:
                    self._pending_child_source_dir = None
                self._source_rows.extend(snapshot.get("references") or ())
                self._atlas_query_has_more = bool(snapshot.get("has_more"))
                self._source_visible_count = len(self._source_rows)
                self.source_more_button.setEnabled(True)
                self._render_sources()
                self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
                self._continue_source_detail_next()
                if (
                    overview_signature != self._source_overview_signature
                    or self._source_query_refresh_failed
                    or self._source_query_refresh_pending
                ):
                    self._request_source_query_refresh()
                return
            if kind == "source_query_refresh":
                run_dir, signature, overview_signature, snapshot = (
                    payload if isinstance(payload, tuple) and len(payload) == 4
                    else (None, None, None, None)
                )
                if error:
                    self._source_page_stale = True
                    self._source_query_refresh_failed = True
                    self.statusBar().showMessage(
                        text["load_failed"].format(reason=str(error)), 8000,
                    )
                    return
                if (
                    run_dir != self._run_dir
                    or signature != self._source_query_signature
                    or signature != self._source_query_context()
                ):
                    return
                if (
                    not isinstance(snapshot, Mapping)
                    or not isinstance(snapshot.get("references"), list)
                ):
                    self._source_page_stale = True
                    self._source_query_refresh_failed = True
                    self.statusBar().showMessage(
                        text["load_failed"].format(reason="invalid source refresh"), 8000,
                    )
                    return
                if self._pending_child_source_dir == run_dir:
                    self._pending_child_source_dir = None
                self._source_query_refresh_failed = False
                self._source_rows = [
                    row for row in snapshot["references"]
                    if isinstance(row, Mapping)
                ]
                self._atlas_query_has_more = bool(snapshot.get("has_more"))
                self._source_visible_count = len(self._source_rows)
                if overview_signature != self._source_overview_signature:
                    self._source_page_stale = True
                self.source_more_button.setEnabled(True)
                self._render_sources()
                self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
                self._continue_source_detail_next()
                if overview_signature != self._source_overview_signature:
                    self._request_source_query_refresh()
                return
            if kind == "latest_resume":
                context = (self._run_dir, getattr(self, "_paper_path", ""))
                if (context != getattr(self, "_atlas_latest_resume_context", None)
                        or self._process is not None or self._commands_process is not None
                        or self.llm_choice_dialog.isVisible()):
                    self._update_run_controls()
                    return
                self._latest_run_row = dict(payload) if isinstance(payload, Mapping) else None
                self._latest_loaded = True
                self._latest_load_error = str(error) if error else None
                self._update_run_controls()
                row, tooltip = self._latest_resume_state()
                if row is not None:
                    self._resume_run_row(row)
                else:
                    QtWidgets.QMessageBox.information(self, text["resume_latest"], tooltip)
                return
            if kind == "latest":
                self._latest_loaded = True
                self._latest_load_error = str(error) if error else None
                self._latest_run_row = dict(payload) if isinstance(payload, Mapping) else None
                self._update_run_controls()
                return
            if error:
                self._show_load_error(kind, str(error))
                return
            if kind in {"history_page", "cache_page"}:
                category = "history" if kind == "history_page" else "cache"
                expected_index = 1 if category == "history" else 2
                if self._page_index != expected_index:
                    return
                offset, page = payload if isinstance(payload, tuple) else (None, None)
                button = self.history_more_button if category == "history" else self.cache_more_button
                button.setEnabled(True)
                if not isinstance(page, Mapping) or not isinstance(page.get("rows"), list):
                    self._show_load_error(category, "invalid inventory page")
                    return
                rows = self._history_rows if category == "history" else self._cache_rows
                if offset != len(rows):
                    return
                combined = [*rows, *page["rows"]]
                if category == "history":
                    self._atlas_history_total = int(page.get("total", len(combined)))
                    self._history_visible_count = len(combined)
                    self._apply_history_rows(combined)
                    self.history_stack.setCurrentIndex(0 if self.history_table.rowCount() else 2)
                    self._queue_history_details()
                else:
                    self._atlas_cache_total = int(page.get("total", len(combined)))
                    self._cache_visible_count = len(combined) + len(self._library_import_rows)
                    self._apply_cache_rows(combined)
                    self.cache_stack.setCurrentIndex(0 if self.cache_table.rowCount() else 2)
                button.setText(text["atlas_load_more"] + f"  ·  {len(combined)}/{page.get("total", len(combined))}")
                button.setVisible(bool(page.get("has_more")))
                return
            if kind == "history_brief" and self._page_index == 1:
                self._apply_history_rows(payload)
                self.history_stack.setCurrentIndex(0 if self.history_table.rowCount() else 2)
                self._queue_history_details()
            elif kind == "history_details" and self._page_index == 1:
                updates = payload if isinstance(payload, Mapping) else {}
                self._apply_history_rows([
                    {**row, **updates.get(str(row.get("run_dir") or ""), {})}
                    for row in self._history_rows
                ])
                self._queue_history_details()
            elif kind == "history" and self._page_index == 1:
                self._apply_history_rows(payload)
                self.history_stack.setCurrentIndex(0 if self.history_table.rowCount() else 2)
            elif kind == "cache" and self._page_index == 2:
                self._apply_cache_rows(payload)
                self.cache_stack.setCurrentIndex(0 if self.cache_table.rowCount() else 2)
            elif kind == "settings" and (self._page_index == 3 or self.llm_choice_dialog.isVisible()):
                self._apply_settings_payload(payload or {})
                self.settings_stack.setCurrentIndex(0)
            elif kind == "docs" and self._page_index == 5:
                self._render_documentation(payload or {"README.md": text["no_documentation"]})
                self.docs_stack.setCurrentIndex(0)
            elif kind == "detail" and self._detail_dialog is not None:
                _dialog, view, row, stack = self._detail_dialog
                details = payload if isinstance(payload, Mapping) else {}
                row["details"] = details
                self._render_source_summary(view, row)
                stack.setCurrentIndex(0)

        def _show_load_error(self, kind: str, reason: str):
            if kind in {"source_page", "source_query"}:
                self._cancel_source_detail_next()
            message = text["load_failed"].format(reason=reason)
            if kind == "snapshot":
                self.phase_label.setText(message)
                self._set_error_state(True)
                self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
            elif kind in {"source_page", "source_query", "source_refresh"}:
                self.source_table.setRowCount(1)
                self.source_table.setItem(0, 1, QtWidgets.QTableWidgetItem(message))
                self.source_more_button.hide()
                self.source_stack.setCurrentIndex(0 if self.source_table.rowCount() else 2)
            elif kind in {"history", "history_brief", "history_details", "history_page"}:
                self.history_more_button.setEnabled(True)
                self.history_table.setRowCount(1)
                self.history_table.setItem(0, 1, QtWidgets.QTableWidgetItem(message))
                self.history_stack.setCurrentIndex(0 if self.history_table.rowCount() else 2)
            elif kind in {"cache", "cache_page"}:
                self.cache_more_button.setEnabled(True)
                self._library_skeleton_effects.clear()
                if self._library_import_rows or self._cache_rows:
                    self._render_cache()
                    self.library_progress.setText(message)
                else:
                    self.cache_table.setRowCount(1)
                    self.cache_table.setItem(0, 1, QtWidgets.QTableWidgetItem(message))
                self.cache_stack.setCurrentIndex(0 if self.cache_table.rowCount() else 2)
                if kind == "cache_page":
                    self.cache_more_button.setText(text.get("atlas_retry", "Retry"))
                    self.cache_more_button.setToolTip(message)
                    self.cache_more_button.show()
            elif kind == "docs":
                self.docs_view.setPlainText(message)
                self.docs_stack.setCurrentIndex(0)
            elif kind == "detail" and self._detail_dialog is not None:
                self._detail_dialog[1].setPlainText(message)
                self._detail_dialog[3].setCurrentIndex(0)
            elif kind == "settings":
                self._settings_loaded = False
                self._refresh_signing_key_controls()
                self.phase_label.setText(message)
                self.settings_stack.setCurrentIndex(0)
            if kind in {"snapshot", "settings"}:
                self._show_phase_activity()

        def _request_snapshot(self, *, force: bool = False):
            if not self._background_loading or not self._run_dir:
                return
            if self._child_database_bootstrap_pending(self._run_dir):
                return
            if self._page_index != 0 and self._pending_finish is None:
                return
            run_dir = self._run_dir
            if self._snapshot_loading_run == run_dir:
                return
            if not force and time.monotonic() < self._next_snapshot_at:
                return
            self._next_snapshot_at = time.monotonic() + 3.0
            self._snapshot_loading_run = run_dir
            self._queue_load(
                "snapshot", lambda run=run_dir: (
                    run, overview_loader(run) if self._source_page_mode else snapshot_loader(run)
                )
            )

        def _apply_theme(self, dark: bool | None = None):
            if dark is not None:
                self._dark = bool(dark)
                QtCore.QSettings("Callimachus", "Callimachus").setValue("dark_theme", self._dark)
            self.theme_toggle.setText(text["theme_light"] if self._dark else text["theme_dark"])
            from .atlante import stylesheet as atlante_stylesheet
            stylesheet = (
                _stylesheet(dark=self._dark, font_size=self._font_size)
                + atlante_stylesheet(dark=self._dark, font_size=self._font_size)
            )
            self.setStyleSheet(stylesheet)
            sync_detail_header = getattr(self, "_atlas_sync_detail_header", None)
            if sync_detail_header is not None:
                sync_detail_header()
            if hasattr(self, "_atlas_reflow_options"):
                self._atlas_reflow_options()
            from .atlas_icons import icon as atlas_icon
            ink = "#afbfdb" if self._dark else "#455374"
            self._refresh_navigation_icons()
            category_icons = {
                "general": "settings", "resolution": "search", "fetch": "download",
                "personalization": "palette",
            }
            for index in range(self.settings_categories.count()):
                key = category_icons.get(self._settings_categories_key(index))
                if key:
                    self.settings_categories.item(index).setIcon(atlas_icon(QtCore, QtGui, key, ink))
            self.theme_toggle.setProperty("atlasIcon", "sun" if self._dark else "moon")
            from core.gui.shell.navigation import set_back_button_presentation
            for widget in self.findChildren(QtWidgets.QAbstractButton):
                if widget.property("atlasBackButton"):
                    set_back_button_presentation(
                        widget, QtCore, QtGui, dark=self._dark,
                        label=widget.accessibleName(), text=widget.text(),
                    )
                    continue
                key = widget.property("atlasIcon")
                if key:
                    size = int(widget.property("atlasIconSize") or max(22, widget.iconSize().width()))
                    if widget.objectName() in {
                        "settingsNavigationToggle", "guideNavigationToggle",
                    }:
                        icon_ink = "#7ab4ff" if self._dark else "#175fe6"
                    elif widget.objectName() == "clearCacheButton":
                        icon_ink = "#ffffff"
                    elif widget.objectName() in {"windowControl", "windowClose"}:
                        icon_ink = "#e6edf6" if self._dark else "#455374"
                    elif widget.objectName() in {"clearBrowserCache", "libraryDeleteSelected"}:
                        icon_ink = "#ef8997" if self._dark else "#be3046"
                    elif widget.objectName() == "openRunReport" or (
                        not self._dark
                        and widget.objectName() in {"runStartButton", "primaryButton"}
                    ):
                        icon_ink = "#ffffff"
                    else:
                        icon_ink = ink
                    widget.setIcon(atlas_icon(QtCore, QtGui, str(key), icon_ink, size))
                    icon_height = 24 if widget.objectName() in {
                        "settingsNavigationToggle", "guideNavigationToggle",
                    } else size
                    widget.setIconSize(QtCore.QSize(size, icon_height))
            manuscript_icon = self.findChild(QtWidgets.QLabel, "manuscriptGlyph")
            if manuscript_icon is not None:
                manuscript_icon.setPixmap(atlas_icon(QtCore, QtGui, "file", ink, 58).pixmap(58, 58))
            for mark in self.findChildren(QtWidgets.QLabel, "serviceGroupIcon"):
                mark.setPixmap(atlas_icon(QtCore, QtGui, str(mark.property("atlasIcon")), ink, 22).pixmap(22, 22))
            for provider_id in getattr(self, "_provider_check_controls", {}):
                self._update_provider_check_controls(provider_id)
            if self._parse_review_window is not None and hasattr(
                self._parse_review_window, "set_dark_theme"
            ):
                self._parse_review_window._font_size = self._font_size
                self._parse_review_window.set_dark_theme(self._dark)
            if self._library_dialog is not None and hasattr(
                self._library_dialog, "set_dark_theme"
            ):
                self._library_dialog.set_dark_theme(self._dark)
            if self._guided_window is not None and hasattr(self._guided_window, "_set_dark_theme"):
                self._guided_window._font_size_px = self._font_size
                self._guided_window._set_dark_theme(self._dark)
            console_font = self.commands_output.font()
            console_font.setPixelSize(max(15, self._font_size + 2))
            self.commands_output.setFont(console_font)
            self._update_brand_logo()
            if hasattr(self, "start_button"):
                self._balance_run_button_geometry()
            for combo in self.findChildren(QtWidgets.QComboBox):
                self._style_combo_popup(combo)
            for name, active in (("appearanceLight", not self._dark), ("appearanceDark", self._dark)):
                for choice in self.findChildren(QtWidgets.QPushButton, name):
                    choice.setChecked(active)
            for name, enabled in (("appearanceSmaller", self._font_size > 11), ("appearanceLarger", self._font_size < 18), ("appearanceReset", self._font_size != 16)):
                for choice in self.findChildren(QtWidgets.QPushButton, name):
                    choice.setEnabled(enabled)
            if hasattr(self, "docs_view"):
                css = _documentation_css(dark=self._dark, font_size=self._font_size)
                self.docs_view.document().setDefaultStyleSheet(css)
                current = getattr(self, "_current_doc_path", "")
                if current and current in getattr(self, "_documents", {}):
                    self.docs_view.setMarkdown(self._documents[current])
                    self._format_documentation_document()
            if hasattr(self, "_atlas_guide"):
                self._atlas_guide.set_theme(self._dark, self._font_size)
            if hasattr(self, "settings_navigation_toggle"):
                self.settings_search.ensurePolished()
                self.settings_navigation_toggle.setFixedHeight(
                    self.settings_search.sizeHint().height()
                )
            if hasattr(self, "_documentation_actions"):
                self._documentation_actions.reflow()
            detail = getattr(self, "_detail_dialog", None)
            if detail is not None and "details" in detail[2]:
                view, row = detail[1], detail[2]
                scroll_position = view.verticalScrollBar().value()
                self._render_source_summary(view, row)
                view.verticalScrollBar().setValue(scroll_position)
            if hasattr(self, "history_actions_widget"):
                reflow = getattr(self, "_atlas_history_reflow", None)
                if reflow is not None:
                    reflow.sync()
                else:
                    self.history_actions_widget.ensurePolished()
                    self.history_actions_widget.layout().activate()
                    self.history_actions_widget.adjustSize()
                    self.history_actions_scroll.setFixedHeight(
                        self.history_actions_widget.sizeHint().height() + 12
                    )
            if hasattr(self, "_atlante_jury_chooser"):
                self._atlante_jury_chooser.apply_theme()
            self._refresh_collection_presentation()
            if hasattr(self, "_analysis_controls_scroll"):
                self._schedule_analysis_checkbox_rewrap()
            self._schedule_settings_caption_alignment()
            if hasattr(self, "_atlas_reflow_startup"):
                self._atlas_reflow_startup()

        def _schedule_analysis_checkbox_rewrap(self):
            if self._analysis_checkbox_rewrap_pending:
                return
            self._analysis_checkbox_rewrap_pending = True
            QtCore.QTimer.singleShot(0, self._update_analysis_checkbox_wrapping)

        def _update_analysis_checkbox_wrapping(self):
            self._analysis_checkbox_rewrap_pending = False
            scroll = getattr(self, "_analysis_controls_scroll", None)
            if scroll is None or scroll.widget() is None or scroll.viewport().width() <= 0:
                return
            viewport = scroll.viewport()
            content = scroll.widget()
            modes = self._analysis_modes_layout
            container = self._analysis_modes_container
            if modes.geometry().width() > 0:
                buttons = list(self.mode_buttons.values())
                column_widths = [
                    max(
                        button.sizeHint().width()
                        for index, button in enumerate(buttons)
                        if index % 2 == column
                    )
                    for column in (0, 1)
                ]
                spacing = modes.horizontalSpacing()
                if spacing < 0:
                    spacing = modes.spacing()
                margins = modes.contentsMargins()
                two_column_width = (
                    sum(column_widths) + spacing + margins.left() + margins.right()
                )
                container_x = container.mapTo(viewport, QtCore.QPoint(0, 0)).x()
                content_x = container.mapTo(content, QtCore.QPoint(0, 0)).x()
                container_trailing_space = max(
                    0, content.width() - content_x - container.width()
                )
                grid_padding = max(0, container.width() - modes.geometry().width())
                available_mode_width = (
                    viewport.width()
                    - container_x
                    - container_trailing_space
                    - grid_padding
                )
                columns = 1 if two_column_width > available_mode_width else 2
                if columns != self._analysis_mode_columns:
                    for index, button in enumerate(buttons):
                        modes.removeWidget(button)
                        modes.addWidget(button, index // columns, index % columns)
                    self._analysis_mode_columns = columns
            for checkbox, full_text in self._analysis_checkbox_labels:
                checkbox_x = checkbox.mapTo(viewport, QtCore.QPoint(0, 0)).x()
                content_x = checkbox.mapTo(content, QtCore.QPoint(0, 0)).x()
                trailing_space = max(
                    0, content.width() - content_x - checkbox.width()
                )
                style = checkbox.style()
                reserved_width = style.pixelMetric(
                    QtWidgets.QStyle.PixelMetric.PM_IndicatorWidth, None, checkbox
                ) + style.pixelMetric(
                    QtWidgets.QStyle.PixelMetric.PM_CheckBoxLabelSpacing, None, checkbox
                )
                text_width = max(
                    1,
                    viewport.width() - checkbox_x - trailing_space - reserved_width,
                )
                metrics = checkbox.fontMetrics()
                lines = []
                line = ""
                for word in full_text.split():
                    candidate = f"{line} {word}" if line else word
                    if line and metrics.horizontalAdvance(candidate) > text_width:
                        lines.append(line)
                        line = word
                    else:
                        line = candidate
                if line:
                    lines.append(line)
                wrapped_text = "\n".join(lines)
                if checkbox.text() != wrapped_text:
                    checkbox.setText(wrapped_text)

        def _style_combo_popup(self, combo):
            view = combo.view()
            palette = view.palette()
            surface = QtGui.QColor("#101722" if self._dark else "#ffffff")
            foreground = QtGui.QColor("#edf3ff" if self._dark else "#18202a")
            highlight = QtGui.QColor("#7aa7e8" if self._dark else "#2457a6")
            for group in (
                QtGui.QPalette.ColorGroup.Active,
                QtGui.QPalette.ColorGroup.Inactive,
                QtGui.QPalette.ColorGroup.Disabled,
            ):
                for role in (
                    QtGui.QPalette.ColorRole.Base,
                    QtGui.QPalette.ColorRole.AlternateBase,
                    QtGui.QPalette.ColorRole.Window,
                ):
                    palette.setColor(group, role, surface)
                for role in (
                    QtGui.QPalette.ColorRole.Text,
                    QtGui.QPalette.ColorRole.WindowText,
                ):
                    palette.setColor(group, role, foreground)
                palette.setColor(group, QtGui.QPalette.ColorRole.Highlight, highlight)
                palette.setColor(
                    group, QtGui.QPalette.ColorRole.HighlightedText,
                    QtGui.QColor("#ffffff"),
                )
            view.setPalette(palette)
            view.viewport().setPalette(palette)
            view.viewport().setAutoFillBackground(True)
            popup = view.window()
            view.setStyleSheet(
                f"QAbstractItemView {{ background-color: {surface.name()}; "
                f"color: {foreground.name()}; selection-background-color: "
                f"{highlight.name()}; selection-color: #ffffff; outline: 0; }}"
            )
            popup.setStyleSheet(
                f"background-color: {surface.name()}; color: {foreground.name()};"
            )
            popup.setPalette(palette)
            popup.setAutoFillBackground(True)

        def _refresh_navigation_icons(self):
            from .atlas_icons import icon
            accent = "#7ab4ff" if self._dark else "#175fe6"
            ink = "#afbfdb" if self._dark else "#455374"
            for index, key in enumerate(("analysis", "history", "library", "settings")):
                self.navigation.item(index).setIcon(icon(
                    QtCore, QtGui, key, accent if index == self.navigation.currentRow() else ink
                ))

        def _load_font_size(self) -> int:
            try:
                value = int(QtCore.QSettings("Callimachus", "Callimachus").value("font_size", 16))
            except (TypeError, ValueError):
                value = 16
            return max(11, min(18, value))

        def _change_font_size(self, delta: int):
            self._font_size = max(11, min(18, self._font_size + delta))
            try:
                QtCore.QSettings("Callimachus", "Callimachus").setValue("font_size", self._font_size)
            except Exception:
                pass
            self._apply_theme(self._dark)

        def eventFilter(self, watched, event):  # noqa: N802
            if (
                watched is getattr(self, "_settings_footer", None)
                and event.type() in (
                    QtCore.QEvent.Type.Resize,
                    QtCore.QEvent.Type.FontChange,
                    QtCore.QEvent.Type.StyleChange,
                )
            ):
                self._update_settings_footer_labels()
            search_results = getattr(self, "settings_search_results", None)
            search = getattr(self, "settings_search", None)
            if (
                watched is search
                and event.type() == QtCore.QEvent.Type.FocusIn
                and search.text().strip()
                and search_results is not None
                and search_results.isHidden()
                and self.pages.currentWidget() is getattr(self, "_settings_page", None)
            ):
                self._refresh_settings_search_results(search.text())
            if (
                search_results is not None
                and not search_results.isHidden()
                and isinstance(watched, QtWidgets.QWidget)
                and watched.window() is self
            ):
                if (
                    event.type() == QtCore.QEvent.Type.KeyPress
                    and event.key() == QtCore.Qt.Key.Key_Escape
                    and self._is_settings_search_control(watched)
                ):
                    self._hide_settings_search_results()
                    return True
                if (
                    event.type() == QtCore.QEvent.Type.MouseButtonPress
                    and not self._is_settings_search_control(watched)
                ):
                    self._hide_settings_search_results()
            if (
                watched is getattr(self, "_settings_page", None)
                or watched is getattr(self, "settings_search", None)
                or watched is getattr(self, "settings_description", None)
            ) and event.type() in (
                QtCore.QEvent.Type.Resize,
                QtCore.QEvent.Type.Move,
                QtCore.QEvent.Type.FontChange,
                QtCore.QEvent.Type.StyleChange,
                QtCore.QEvent.Type.Show,
            ):
                self._schedule_settings_search_item_sizes()
            if (
                watched is getattr(self, "_settings_page", None)
                and event.type() == QtCore.QEvent.Type.Hide
            ):
                self._hide_settings_search_results()
            if search_results is not None and watched is search_results and event.type() in (
                QtCore.QEvent.Type.Resize,
                QtCore.QEvent.Type.FontChange,
                QtCore.QEvent.Type.StyleChange,
            ):
                self._schedule_settings_search_item_sizes()
            if event.type() in (
                QtCore.QEvent.Type.Resize,
                QtCore.QEvent.Type.FontChange,
                QtCore.QEvent.Type.StyleChange,
                QtCore.QEvent.Type.Show,
            ) and isinstance(watched, QtWidgets.QLabel):
                try:
                    if watched.objectName().startswith("settingCaption"):
                        self._schedule_settings_caption_alignment()
                except RuntimeError:
                    pass
            if (
                isinstance(watched, QtWidgets.QWidget)
                and event.type() in (
                    QtCore.QEvent.Type.Resize,
                    QtCore.QEvent.Type.Move,
                )
            ):
                try:
                    if getattr(watched, "_settings_caption_alignment_watched", False):
                        self._schedule_settings_caption_alignment()
                except RuntimeError:
                    pass
            settings_scroll = getattr(self, "settings_scroll", None)
            if (
                settings_scroll is not None
                and watched is getattr(self, "_settings_scroll_viewport", None)
                and event.type() == QtCore.QEvent.Type.Resize
            ):
                self._schedule_settings_caption_alignment()
            if event.type() == QtCore.QEvent.Type.Resize and watched in getattr(self, "jury_model_buttons", {}).values():
                self._refresh_jury_model_buttons()
            if event.type() == QtCore.QEvent.Type.Resize and watched in (getattr(self, "_jury_action_role_buttons", None) or {}).values():
                self._refresh_jury_action_role_buttons()
            analysis_scroll = getattr(self, "_analysis_controls_scroll", None)
            if analysis_scroll is not None:
                if (
                    watched is analysis_scroll.viewport()
                    and event.type() == QtCore.QEvent.Type.Resize
                ) or (
                    any(
                        watched is widget
                        for widget in (
                            *(checkbox for checkbox, _text in self._analysis_checkbox_labels),
                            *self.mode_buttons.values(),
                        )
                    )
                    and event.type() in (
                        QtCore.QEvent.Type.FontChange,
                        QtCore.QEvent.Type.StyleChange,
                    )
                ):
                    self._schedule_analysis_checkbox_rewrap()
            checkbox = getattr(self, "_llm_checkbox_cells", {}).get(watched)
            if checkbox is not None and event.type() in (
                QtCore.QEvent.Type.MouseButtonPress,
                QtCore.QEvent.Type.MouseButtonRelease,
            ) and event.button() == QtCore.Qt.MouseButton.LeftButton:
                if event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                    checkbox.click()
                return True
            if (
                isinstance(watched, QtWidgets.QLineEdit)
                and watched.property("secretConfigured")
                and watched.isReadOnly()
                and event.type() == QtCore.QEvent.Type.MouseButtonPress
            ):
                self._begin_secret_replacement(str(watched.property("secretName")), watched)
                return True
            if watched is self.paper_drop and event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    self._browse_paper()
                    return True
            if watched is self.paper_drop and event.type() == QtCore.QEvent.Type.KeyPress:
                if event.key() in (
                    QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter, QtCore.Qt.Key.Key_Space
                ):
                    self._browse_paper()
                    return True
            if watched is self.paper_drop and event.type() == QtCore.QEvent.Type.DragEnter:
                from core.parse.extract import supported_extensions
                urls = event.mimeData().urls()
                if (len(urls) == 1 and urls[0].isLocalFile()
                        and Path(urls[0].toLocalFile()).suffix.casefold() in supported_extensions()):
                    event.acceptProposedAction()
                    return True
            if watched is self.paper_drop and event.type() == QtCore.QEvent.Type.Drop:
                from core.parse.extract import supported_extensions
                urls = event.mimeData().urls()
                if (len(urls) == 1 and urls[0].isLocalFile()
                        and Path(urls[0].toLocalFile()).suffix.casefold() in supported_extensions()):
                    self._set_paper(urls[0].toLocalFile())
                    event.acceptProposedAction()
                    return True
            return super().eventFilter(watched, event)

        def _browse_paper(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                text["select_paper"],
                filter=(
                    _document_file_filter(text["manuscripts"]) + ";;" +
                    f"{text['all_files']} (*)"
                ),
            )
            if path:
                self._set_paper(path)

        def _set_paper(self, path: str):
            self._paper_path = path
            self.paper_drop.setText(Path(path).name if path else text["drop_paper"])
            if hasattr(self, "new_analysis_button"):
                self._update_run_controls()

        def _new_analysis(self):
            self._invalidate_load("latest_resume")
            if (
                self._library_busy
                or self._new_analysis_reset_pending
                or self._institutional_launch_request is not None
                or self._commands_process is not None
                or self._guided_window is not None
                or self._parse_review_window is not None
            ):
                return
            if self._process is not None:
                if self._stopping:
                    return
                self._new_analysis_reset_pending = True
                if not self._request_process_stop():
                    self._new_analysis_reset_pending = False
                    self._update_run_controls()
                return
            self._clear_analysis()

        def _clear_analysis(self):
            """Clear only the Analysis view; run files and History remain durable."""
            if (
                self._process is not None
                or self._institutional_launch_request is not None
            ):
                return False
            self._cancel_source_detail_next()
            if self._detail_dialog is not None:
                self._detail_dialog[0].close()
            reset_overlay = getattr(self, "_atlas_reset_source_detail_overlay", None)
            if reset_overlay is not None:
                reset_overlay()
            jury_readiness_cancelled = self._jury_readiness_request is not None
            self._jury_readiness_request = None
            self._institutional_retry_pending = None
            self._institutional_login_deferred = False
            self._invalidate_load("jury_readiness")

            self._run_dir = None
            self._new_analysis_reset_pending = False
            self._pending_finish = None
            self._auto_open_report_on_finish = True
            self._stopping = False
            self._cancel_stop_escalation()
            self._forced_stop_process = None
            self._cooldown_until = None
            self._cooldown_activity = ""
            self.cooldown_status.hide()

            if self._background_loading:
                self._atlas_source_cancel.set()
                for kind in (
                    "snapshot", "source_page", "source_refresh",
                    "source_query", "source_query_refresh",
                ):
                    self._invalidate_load(kind)
            self._snapshot_loading_run = None
            self._snapshot_loaded_run = None
            self._next_snapshot_at = 0.0
            self._source_query_timer.stop()
            self._source_rows = []
            self._source_paged_rows = []
            self._source_paged_total = 0
            self._source_next_offset = 0
            self._source_page_loaded_run = None
            self._source_query_signature = None
            self._source_query_refresh_failed = False
            self._source_query_refresh_pending = False
            self._source_overview_signature = None
            self._source_page_stale = False
            self._source_visible_count = self._page_batch_size(self.source_table)
            self._source_sort_column = None
            self._source_sort_ascending = True
            self._pending_child_source_dir = None
            self.source_search.clear()
            self.source_filter.setCurrentIndex(0)
            self.source_phase_filter.setCurrentIndex(0)
            self.source_table.setRowCount(0)
            self.source_table.clearSelection()
            self.source_table.horizontalHeader().setSortIndicator(
                -1, QtCore.Qt.SortOrder.AscendingOrder
            )
            self.source_more_button.hide()
            self.source_stack.setCurrentWidget(self.source_table)

            self._set_paper("")
            self._display_run_done = False
            self._display_references_only = None
            self._guided_fetch_available = False
            self._parse_review_available = False
            self._parse_review_pending = 0
            self._last_phase_progress = None
            self._set_error_state(False)
            self.phase_label.setToolTip("")
            self.phase_label.setText(text["phase_waiting"])
            self.phase_progress.setRange(0, 100)
            self.phase_progress.setValue(0)
            self.phase_progress.setFormat(text["phase_progress"])
            self.phase_progress_percent.setText("0%")
            self.current_activity.setText(
                text["current_activity"].format(activity=text["activity_waiting"])
            )
            if jury_readiness_cancelled:
                changed = text["jury_readiness_changed"]
                self.llm_status.setText(changed)
                self.phase_label.setText(changed)
                self.current_activity.setText(
                    text["current_activity"].format(activity=changed)
                )
            self._update_run_controls()
            self._update_history_actions()
            return True

        def _selected_mode(self) -> str:
            button = self.mode_group.checkedButton()
            return str(button.property("mode")) if button is not None else "standard"

        def _selected_jury_level(self) -> str:
            button = self.jury_group.checkedButton()
            return str(button.property("jury2_level")) if button is not None else "medium"

        def _set_commands_status(self, value: str, *, error: bool = False):
            self._console_load_error = False
            self.commands_status_label.setText(value)
            self.commands_status_label.setProperty("error", error)
            self.commands_status_label.setVisible(bool(value))
            self.commands_status_label.style().unpolish(self.commands_status_label)
            self.commands_status_label.style().polish(self.commands_status_label)

        def _clear_console_load_error(self):
            if self._console_load_error:
                self._set_commands_status("")
                self._console_load_error = False

        def _reset_console_document_state(self):
            self._console_loaded_start = 0
            self._console_history_end_offset = 0
            self._console_display_end = 0
            self._console_live_start = 0
            self._console_live_start_offset = 0
            self._console_live_text = ""
            self._console_page_loading = False
            self._console_page_kind = None

        @staticmethod
        def _console_utf16_length(value: str) -> int:
            return len(value.encode("utf-16-le")) // 2

        def _update_console_history_controls(self):
            has_older = bool(
                self._page_index == 4
                and self._console_loaded
                and self._console_log_path is not None
                and self._console_loaded_start > 0
            )
            has_gap = bool(
                self._page_index == 4
                and self._console_loaded
                and self._console_log_path is not None
                and self._console_history_end_offset
                < self._console_live_start_offset
            )
            loading_older = (
                self._console_page_loading and self._console_page_kind == "older"
            )
            loading_gap = (
                self._console_page_loading and self._console_page_kind == "gap"
            )
            self.commands_history_button.setVisible(has_older or loading_older)
            self.commands_history_button.setEnabled(
                has_older and not self._console_page_loading
            )
            self.commands_history_button.setText(
                text["commands_history_loading"]
                if loading_older
                else text["commands_load_earlier"]
            )
            self.commands_gap_button.setVisible(has_gap or loading_gap)
            self.commands_gap_button.setEnabled(
                has_gap and not self._console_page_loading
            )
            self.commands_gap_button.setText(
                text["commands_gap_loading"]
                if loading_gap
                else text["commands_load_missing"]
            )
            self.console_history_spinner.setAccessibleName(
                text["commands_gap_loading"]
                if loading_gap
                else text["commands_history_loading"]
            )
            self.console_history_spinner.setVisible(self._console_page_loading)
            self.commands_export_button.setEnabled(
                self._console_log_path is not None
            )

        def _on_console_scroll_changed(self, value: int):
            if (
                self._page_index != 4
                or not self._console_loaded
                or self._console_page_loading
                or self._console_loaded_start <= 0
            ):
                return
            scrollbar = self.commands_output.verticalScrollBar()
            if value <= scrollbar.minimum() + 1:
                QtCore.QTimer.singleShot(0, self._load_older_console_page)

        def _load_older_console_page(self):
            if (
                self._page_index != 4
                or not self._console_loaded
                or self._console_page_loading
                or self._console_loaded_start <= 0
            ):
                return
            path = self._console_log_path
            if path is None:
                return
            generation = self._console_generation
            end_offset = self._console_loaded_start
            self._console_page_loading = True
            self._console_page_kind = "older"
            self._update_console_history_controls()
            loader = lambda: self._read_console_history_page(
                path, generation, end_offset
            )
            if self._background_loading:
                self._queue_load("console_older", loader)
                return
            try:
                self._apply_console_history_page(loader())
            except OSError as exc:
                self._set_commands_status(
                    text["load_failed"].format(reason=exc), error=True
                )
                self._console_load_error = True
            finally:
                self._console_page_loading = False
                self._console_page_kind = None
                self._update_console_history_controls()

        def _load_console_gap_page(self):
            if (
                self._page_index != 4
                or not self._console_loaded
                or self._console_page_loading
                or self._console_history_end_offset
                >= self._console_live_start_offset
            ):
                return
            path = self._console_log_path
            if path is None:
                return
            generation = self._console_generation
            start_offset = self._console_history_end_offset
            end_offset = min(
                start_offset + _CONSOLE_PAGE_BYTES,
                self._console_live_start_offset,
            )
            if end_offset <= start_offset:
                return
            self._console_page_loading = True
            self._console_page_kind = "gap"
            self._update_console_history_controls()
            loader = lambda: self._read_console_gap_page(
                path, generation, start_offset, end_offset
            )
            if self._background_loading:
                self._queue_load("console_gap", loader)
                return
            try:
                self._apply_console_gap_page(loader())
            except OSError as exc:
                self._set_commands_status(
                    text["load_failed"].format(reason=exc), error=True
                )
                self._console_load_error = True
            finally:
                self._console_page_loading = False
                self._console_page_kind = None
                self._update_console_history_controls()

        def _read_console_page(
            self,
            path: Path,
            generation: int,
            *,
            end_offset: int | None = None,
            limit_bytes: int = _CONSOLE_PAGE_BYTES,
            start_offset: int | None = None,
            preserve_start: bool = False,
        ):
            try:
                with self._console_log_lock:
                    if (
                        generation != self._console_generation
                        or path != self._console_log_path
                    ):
                        return "", 0, 0, generation
                    file_size = path.stat().st_size
                end = file_size if end_offset is None else min(end_offset, file_size)
                requested_start = max(0, end - limit_bytes)
                read_start = (
                    min(max(0, start_offset), end)
                    if start_offset is not None
                    else requested_start
                )
                with path.open("rb") as stream:
                    if preserve_start and end < file_size:
                        stream.seek(end)
                        following = stream.read(1)
                        while following and following[0] & 0xC0 == 0x80:
                            end -= 1
                            stream.seek(end)
                            following = stream.read(1)
                    stream.seek(read_start)
                    payload = stream.read(max(0, end - read_start))
                skipped = 0
                if read_start > 0 and not preserve_start:
                    newline = payload.find(b"\n")
                    if newline >= 0 and newline + 1 < len(payload):
                        # Keep page boundaries between lines so a prepend does
                        # not change the wrapping of already visible content.
                        skipped = newline + 1
                    else:
                        # A single oversized line, or a page ending at a
                        # newline, must still make progress from a valid UTF-8
                        # code point boundary.
                        while (
                            skipped < len(payload)
                            and payload[skipped] & 0xC0 == 0x80
                        ):
                            skipped += 1
                start = read_start + skipped
                return (
                    payload[skipped:].decode("utf-8", errors="replace"),
                    start,
                    end,
                    generation,
                )
            finally:
                with self._console_log_lock:
                    retired = path != self._console_log_path
                if retired:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

        def _begin_console_capture(self):
            if self._background_loading:
                self._invalidate_load("console")
                self._invalidate_load("console_older")
                self._invalidate_load("console_gap")
            self._console_capture_error = None
            self._console_load_error = False
            self._console_page_loading = False
            previous_path = self._console_log_path
            try:
                with self._console_log_lock:
                    self._console_generation += 1
                    log_dir = Path(user_data_root()) / "logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    descriptor, name = tempfile.mkstemp(
                        prefix="callimachus-console-", suffix=".log", dir=log_dir
                    )
                    os.close(descriptor)
                    self._console_log_path = Path(name)
                    if previous_path is not None:
                        try:
                            previous_path.unlink(missing_ok=True)
                        except OSError:
                            pass
            except OSError as exc:
                with self._console_log_lock:
                    self._console_log_path = None
                    if previous_path is not None:
                        try:
                            previous_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                self._console_capture_error = str(exc)
            self._console_loaded = False
            self.commands_output.clear()
            self._reset_console_document_state()
            self._update_console_history_controls()
            if self._console_capture_error:
                self._console_loaded = self._page_index == 4
                self._set_commands_status(
                    text["commands_capture_error"].format(
                        reason=self._console_capture_error
                    ),
                    error=True,
                )
                if self._page_index == 4:
                    self.console_stack.setCurrentWidget(self.commands_output)
            elif self._page_index == 4:
                self._set_commands_status("")
                self.console_stack.setCurrentIndex(1)
                self._load_console_capture()
            else:
                self._set_commands_status("")

        def _read_console_snapshot(self, path: Path, generation: int):
            return self._read_console_page(path, generation)

        def _read_console_history_page(
            self, path: Path, generation: int, end_offset: int
        ):
            return self._read_console_page(
                path, generation, end_offset=end_offset
            )

        def _read_console_gap_page(
            self,
            path: Path,
            generation: int,
            start_offset: int,
            end_offset: int,
        ):
            return self._read_console_page(
                path,
                generation,
                end_offset=end_offset,
                limit_bytes=end_offset - start_offset,
                start_offset=start_offset,
                preserve_start=True,
            )

        def _load_console_capture(self):
            if self._console_loaded:
                self.console_stack.setCurrentWidget(self.commands_output)
                return
            path = self._console_log_path
            if path is None:
                self._console_loaded = True
                self.console_stack.setCurrentWidget(self.commands_output)
                self._update_console_history_controls()
                return
            self.console_stack.setCurrentIndex(1)
            generation = self._console_generation
            loader = lambda path=path, generation=generation: self._read_console_snapshot(
                path, generation
            )
            if self._background_loading:
                self._queue_load("console", loader)
                return
            try:
                self._apply_console_snapshot(loader())
            except OSError as exc:
                self._set_commands_status(
                    text["load_failed"].format(reason=exc), error=True
                )
                self._console_load_error = True
                self.console_stack.setCurrentWidget(self.commands_output)

        def _apply_console_snapshot(self, payload):
            content, start_offset, loaded_end, generation = payload
            if (
                self._page_index != 4
                or generation != self._console_generation
                or self._console_log_path is None
            ):
                return
            with self._console_log_lock:
                path = self._console_log_path
                if generation != self._console_generation or path is None:
                    return
                end_offset = path.stat().st_size
            if end_offset - loaded_end > _CONSOLE_LIVE_WINDOW_BYTES:
                content, start_offset, loaded_end, generation = self._read_console_page(
                    path,
                    generation,
                    limit_bytes=_CONSOLE_LIVE_WINDOW_BYTES,
                )
                end_offset = loaded_end
            with path.open("rb") as stream:
                stream.seek(loaded_end)
                trailing = stream.read(max(0, end_offset - loaded_end))
            self.commands_output.setPlainText(content)
            self._console_loaded_start = start_offset
            self._console_history_end_offset = start_offset
            self._console_display_end = loaded_end
            self._console_live_start = 0
            self._console_live_start_offset = start_offset
            self._console_live_text = content
            if trailing:
                self._insert_console_text(
                    trailing.decode("utf-8", errors="replace")
                )
            self._console_display_end = end_offset
            self._console_loaded = True
            self._console_page_loading = False
            self.commands_output.moveCursor(QtGui.QTextCursor.MoveOperation.End)
            scrollbar = self.commands_output.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            self.console_stack.setCurrentWidget(self.commands_output)
            self._clear_console_load_error()
            self._update_console_history_controls()

        def _apply_console_history_page(self, payload):
            content, start_offset, end_offset, generation = payload
            if (
                self._page_index != 4
                or not self._console_loaded
                or generation != self._console_generation
                or self._console_log_path is None
                or end_offset > self._console_loaded_start
                or not content
            ):
                return
            scrollbar = self.commands_output.verticalScrollBar()
            old_value = scrollbar.value()
            old_maximum = scrollbar.maximum()
            cursor = QtGui.QTextCursor(self.commands_output.document())
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
            cursor.insertText(content)
            inserted_units = self._console_utf16_length(content)
            if self._console_live_start == 0:
                # Live output may have trimmed while this first history page
                # was loading. Its saved end is the actual prefix boundary.
                self._console_history_end_offset = end_offset
            self._console_live_start += inserted_units
            self._console_loaded_start = start_offset
            scrollbar.setValue(old_value + scrollbar.maximum() - old_maximum)
            self._clear_console_load_error()
            self._update_console_history_controls()

        def _apply_console_gap_page(self, payload):
            content, start_offset, end_offset, generation = payload
            if (
                self._page_index != 4
                or not self._console_loaded
                or generation != self._console_generation
                or self._console_log_path is None
                or start_offset != self._console_history_end_offset
                or end_offset > self._console_live_start_offset
                or end_offset <= start_offset
                or not content
            ):
                return
            scrollbar = self.commands_output.verticalScrollBar()
            old_value = scrollbar.value()
            old_maximum = scrollbar.maximum()
            insertion_position = self._console_live_start
            anchor = self.commands_output.cursorForPosition(
                QtCore.QPoint(2, 2)
            )
            anchor_position = anchor.position()
            cursor = QtGui.QTextCursor(self.commands_output.document())
            cursor.setPosition(insertion_position)
            cursor.insertText(content)
            inserted_units = self._console_utf16_length(content)
            self._console_live_start += inserted_units
            self._console_history_end_offset = end_offset
            if anchor_position >= insertion_position:
                scrollbar.setValue(
                    old_value + scrollbar.maximum() - old_maximum
                )
            else:
                scrollbar.setValue(old_value)
            self._clear_console_load_error()
            self._update_console_history_controls()

        def _append_console_output(self, output: str):
            if not output:
                return
            payload = output.encode("utf-8")
            try:
                with self._console_log_lock:
                    path = self._console_log_path
                    if path is not None:
                        with path.open("ab") as stream:
                            stream.write(payload)
                            stream.flush()
            except OSError as exc:
                self._console_capture_error = str(exc)
                self._set_commands_status(
                    text["commands_capture_error"].format(reason=exc), error=True
                )
                with self._console_log_lock:
                    self._console_generation += 1
                    self._console_log_path = None
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                if self._background_loading:
                    self._invalidate_load("console")
                    self._invalidate_load("console_older")
                    self._invalidate_load("console_gap")
                if self._page_index == 4:
                    self._console_loaded = True
                    cursor = self.commands_output.textCursor()
                    cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
                    self._console_live_start = cursor.position()
                    self._console_live_text = ""
                    self._console_live_start_offset = 0
                    self._console_loaded_start = 0
                    self._console_history_end_offset = 0
                    self._update_console_history_controls()
                    self.console_stack.setCurrentWidget(self.commands_output)
            else:
                if path is not None and self._console_loaded:
                    self._console_display_end += len(payload)
            if self._page_index == 4 and self._console_loaded:
                self._insert_console_text(output)

        def _insert_console_text(self, output: str):
            if not output:
                return
            combined = self._console_live_text + output
            encoded = combined.encode("utf-8")
            removed_bytes = max(0, len(encoded) - _CONSOLE_LIVE_WINDOW_BYTES)
            while removed_bytes < len(encoded) and encoded[removed_bytes] & 0xC0 == 0x80:
                removed_bytes += 1
            removed_prefix = encoded[:removed_bytes].decode("utf-8", errors="replace")
            live_text = encoded[removed_bytes:].decode("utf-8", errors="replace")
            removed_units = self._console_utf16_length(removed_prefix)

            scrollbar = self.commands_output.verticalScrollBar()
            follows_output = scrollbar.value() >= scrollbar.maximum() - 1
            anchor = self.commands_output.cursorForPosition(QtCore.QPoint(2, 2))
            anchor_position = anchor.position()
            insert = self.commands_output.textCursor()
            insert.movePosition(QtGui.QTextCursor.MoveOperation.End)
            insert.insertText(output)
            if removed_units:
                trim = QtGui.QTextCursor(self.commands_output.document())
                trim.setPosition(self._console_live_start)
                trim.setPosition(
                    self._console_live_start + removed_units,
                    QtGui.QTextCursor.MoveMode.KeepAnchor,
                )
                trim.removeSelectedText()
                if anchor_position >= self._console_live_start:
                    anchor_position = max(
                        self._console_live_start,
                        anchor_position - removed_units,
                    )
                self._console_live_start_offset += removed_bytes
                if self._console_live_start == 0:
                    self._console_loaded_start = self._console_live_start_offset
                    self._console_history_end_offset = (
                        self._console_live_start_offset
                    )
            self._console_live_text = live_text
            if follows_output:
                self.commands_output.moveCursor(QtGui.QTextCursor.MoveOperation.End)
                scrollbar.setValue(scrollbar.maximum())
            else:
                anchor.setPosition(anchor_position)
                self.commands_output.setTextCursor(anchor)
                self.commands_output.ensureCursorVisible()
                scrollbar.setValue(min(scrollbar.value(), scrollbar.maximum()))
            self._update_console_history_controls()

        def _choose_console_export(self):
            destination, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
                self,
                text["commands_export_title"],
                "",
                text["commands_export_filter"],
            )
            if destination:
                self._export_console_log_to(Path(destination))

        def _export_console_log_to(self, destination: Path) -> bool:
            try:
                with self._console_log_lock:
                    path = self._console_log_path
                    if path is None:
                        raise OSError(text["commands_export_unavailable"])
                    size = path.stat().st_size
                if path.resolve() == destination.resolve():
                    raise OSError(text["commands_export_same_file"])
                remaining = size
                with path.open("rb") as source, destination.open("wb") as target:
                    while remaining:
                        chunk = source.read(
                            min(_CONSOLE_EXPORT_CHUNK_BYTES, remaining)
                        )
                        if not chunk:
                            raise OSError(text["commands_export_incomplete"])
                        target.write(chunk)
                        remaining -= len(chunk)
                self._set_commands_status(
                    text["commands_export_done"].format(path=destination),
                    error=False,
                )
                return True
            except OSError as exc:
                self._set_commands_status(
                    text["commands_export_error"].format(reason=exc),
                    error=True,
                )
                return False

        def _cleanup_console_capture(self):
            with self._console_log_lock:
                path = self._console_log_path
                self._console_log_path = None
                self._console_generation += 1
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            self._console_loaded = False
            self._console_capture_error = None
            self.commands_output.clear()
            self._reset_console_document_state()
            self._update_console_history_controls()

        def _run_commands(self):
            if self._library_busy:
                self._set_commands_status(text["library_working"], error=True)
                return
            if self._process is not None:
                self._set_commands_status(text["commands_status_busy_analysis"], error=True)
                return
            if self._guided_window is not None:
                self._set_commands_status(text["commands_status_busy_guided"], error=True)
                return
            if self._commands_process is not None:
                self._set_commands_status(text["commands_status_busy_commands"], error=True)
                return

            arguments, error_key = _parse_commands_arguments(
                self.commands_input.text(),
                displayed_prefix=self._commands_displayed_prefix,
            )
            if error_key is not None:
                self._set_commands_status(text[error_key], error=True)
                return

            try:
                command = application_argv(*arguments, source_root=resource_root())
                working_directory = user_data_root() if is_frozen() else resource_root()
            except (OSError, RuntimeError, ValueError) as exc:
                self._set_commands_status(
                    text["commands_status_start_failed"].format(reason=str(exc)),
                    error=True,
                )
                return

            self._begin_console_capture()
            process = QtCore.QProcess(self)
            self._commands_process = process
            self._commands_stdout_decoder = codecs.getincrementaldecoder("utf-8")(
                errors="replace"
            )
            self._commands_stderr_decoder = codecs.getincrementaldecoder("utf-8")(
                errors="replace"
            )
            process.setWorkingDirectory(str(working_directory))
            process_environment = QtCore.QProcessEnvironment.systemEnvironment()
            for name in self._settings_environment_names:
                process_environment.remove(name)
            process_environment.insert("PYTHONIOENCODING", "utf-8:replace")
            process.setProcessEnvironment(process_environment)
            process.readyReadStandardOutput.connect(
                lambda process=process: self._read_commands_output(process, stderr=False)
            )
            process.readyReadStandardError.connect(
                lambda process=process: self._read_commands_output(process, stderr=True)
            )
            process.finished.connect(
                lambda exit_code, exit_status, process=process:
                self._on_commands_finished(process, exit_code, exit_status)
            )
            process.errorOccurred.connect(
                lambda error, process=process:
                self._on_commands_process_error(process, error)
            )
            self._set_commands_status(
                text["commands_capture_error"].format(
                    reason=self._console_capture_error
                ) if self._console_capture_error
                else text["commands_status_running"],
                error=bool(self._console_capture_error),
            )
            self._update_run_controls()
            self._update_history_actions()
            process.start(command[0], list(command[1:]))
            process.closeWriteChannel()

        def _read_commands_output(self, process, *, stderr: bool, final: bool = False):
            if process is not self._commands_process:
                return
            data = (
                process.readAllStandardError()
                if stderr else process.readAllStandardOutput()
            )
            decoder_name = (
                "_commands_stderr_decoder" if stderr
                else "_commands_stdout_decoder"
            )
            decoder = getattr(self, decoder_name)
            if decoder is None:
                return
            output = decoder.decode(bytes(data), final=final)
            if final:
                setattr(self, decoder_name, None)
            if not output:
                return
            if stderr:
                output = text["commands_stderr_prefix"].format(output=output)
            self._append_console_output(output)

        def _on_commands_process_error(self, process, error):
            if process is not self._commands_process:
                return
            if error == QtCore.QProcess.ProcessError.FailedToStart:
                self._set_commands_status(
                    text["commands_status_start_failed"].format(
                        reason=process.errorString()
                    ),
                    error=True,
                )
                self._commands_process = None
                self._commands_stdout_decoder = None
                self._commands_stderr_decoder = None
                process.deleteLater()
                self._update_run_controls()
                self._update_history_actions()
            else:
                self._set_commands_status(
                    text["commands_status_error"].format(reason=process.errorString()),
                    error=True,
                )

        def _on_commands_finished(self, process, exit_code: int, exit_status):
            if process is not self._commands_process:
                return
            self._read_commands_output(process, stderr=False)
            self._read_commands_output(process, stderr=True)
            self._read_commands_output(process, stderr=False, final=True)
            self._read_commands_output(process, stderr=True, final=True)
            normal_exit = exit_status == QtCore.QProcess.ExitStatus.NormalExit
            status = text[
                "commands_status_exit" if normal_exit else "commands_status_crashed"
            ].format(exit_code=exit_code)
            if self._console_capture_error:
                status += "\n" + text["commands_capture_error"].format(
                    reason=self._console_capture_error
                )
            self._set_commands_status(
                status,
                error=not normal_exit or exit_code != 0 or bool(self._console_capture_error),
            )
            self._commands_process = None
            process.deleteLater()
            self._update_run_controls()
            self._update_history_actions()
            self._open_deferred_institutional_login()

        def _confirm_no_jury_run(self) -> bool:
            dialog = QtWidgets.QMessageBox(self)
            dialog.setObjectName("noJuryConfirmationDialog")
            dialog.setIcon(QtWidgets.QMessageBox.Icon.Question)
            QtWidgets.QWidget.setWindowTitle(
                dialog, text["no_jury_confirmation_title"]
            )
            dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            dialog.setText(text["no_jury_confirmation_body"])
            references_only = dialog.addButton(
                text["references_check_only"],
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )
            cancel = dialog.addButton(
                text["cancel_action"], QtWidgets.QMessageBox.ButtonRole.RejectRole,
            )
            dialog.setDefaultButton(cancel)
            dialog.setEscapeButton(cancel)
            from .atlas_icons import icon as atlas_icon
            icon_color = "#e6edf6" if self._dark else "#11183c"
            dialog.setIconPixmap(
                atlas_icon(QtCore, QtGui, "help", color=icon_color, size=36).pixmap(
                    QtCore.QSize(36, 36)
                )
            )
            self._prepare_confirmation_dialog(dialog)
            dialog.exec()
            return dialog.clickedButton() is references_only

        def _confirm_one_jury_run(self, missing_role: str) -> str:
            configured_role = "jury1" if missing_role == "jury2" else "jury2"
            configured_label = text[f"{configured_role}_role"]
            missing_label = text[f"{missing_role}_role"]
            dialog = QtWidgets.QMessageBox(self)
            dialog.setObjectName("oneJuryConfirmationDialog")
            dialog.setIcon(QtWidgets.QMessageBox.Icon.Question)
            QtWidgets.QWidget.setWindowTitle(
                dialog,
                text["one_jury_confirmation_title"].format(
                    configured_role=configured_label,
                ),
            )
            dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            dialog.setText(text["one_jury_confirmation_body"].format(
                configured_role=configured_label, missing_role=missing_label,
            ))
            choose = dialog.addButton(
                text["choose_missing_jury"].format(role=missing_label),
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )
            references_only = dialog.addButton(
                text["references_check_only"],
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )
            cancel = dialog.addButton(
                text["cancel_action"], QtWidgets.QMessageBox.ButtonRole.RejectRole,
            )
            dialog.setDefaultButton(cancel)
            dialog.setEscapeButton(cancel)
            from .atlas_icons import icon as atlas_icon
            icon_color = "#e6edf6" if self._dark else "#11183c"
            dialog.setIconPixmap(
                atlas_icon(QtCore, QtGui, "help", color=icon_color, size=36).pixmap(
                    QtCore.QSize(36, 36)
                )
            )
            self._prepare_confirmation_dialog(dialog)
            dialog.exec()
            if dialog.clickedButton() is choose:
                return "choose"
            if dialog.clickedButton() is references_only:
                return "references_only"
            return "cancel"

        def _prepare_confirmation_dialog(self, dialog):
            # QMessageBox.setWindowTitle() is ignored on macOS; the custom
            # dialog frame reads the inherited QWidget title property.
            maximum_width = max(280, self.width() - 48)
            dialog.setStyleSheet(self.styleSheet())
            dialog.setFont(self.font())
            message = dialog.findChild(QtWidgets.QLabel, "qt_msgbox_label")
            if message is not None:
                message.setWordWrap(True)
                message.setMinimumWidth(0)
                message.setMaximumWidth(max(120, maximum_width - 144))
                def wrap_after_show(d=dialog, max_width=maximum_width):
                    label = d.findChild(QtWidgets.QLabel, "qt_msgbox_label")
                    layout = d.layout()
                    if label is None or layout is None:
                        return
                    label.setWordWrap(True)
                    label.setMinimumWidth(0)

                    # QMessageBox can restore its natural width in showEvent.
                    # Measure its outer overhead and fit the wrapped text to
                    # the remaining layout width instead of clipping buttons.
                    layout_width = max(0, layout.sizeHint().width())
                    dialog_width = max(d.width(), d.sizeHint().width())
                    outer_width = max(0, dialog_width - layout_width)
                    available_layout_width = max(0, max_width - outer_width)
                    current_label_width = label.maximumWidth()
                    other_layout_width = max(0, layout_width - current_label_width)
                    available_label_width = max(
                        0, available_layout_width - other_layout_width
                    )
                    if available_label_width < current_label_width:
                        label.setMaximumWidth(available_label_width)
                        d.setMinimumSize(0, 0)
                        d.setMaximumSize(max_width, 16777215)
                        layout.invalidate()
                        layout.activate()
                        d.adjustSize()
                QtCore.QTimer.singleShot(0, dialog, wrap_after_show)
            button_box = dialog.findChild(QtWidgets.QDialogButtonBox)
            if button_box is not None:
                buttons = list(dialog.buttons())
                spacing = button_box.layout().spacing() if button_box.layout() else 0
                buttons_width = sum(button.sizeHint().width() for button in buttons)
                buttons_width += max(0, len(buttons) - 1) * max(0, spacing)
                if buttons_width > max(140, maximum_width - 40):
                    button_box.setOrientation(QtCore.Qt.Orientation.Vertical)
                    button_box.setMaximumWidth(max(160, maximum_width - 64))
                    for button in buttons:
                        button.setMaximumWidth(max(160, maximum_width - 64))
            dialog.setMinimumWidth(min(520, maximum_width))
            dialog.setMaximumWidth(maximum_width)

        def _start_analysis(self):
            if (
                self._commands_process is not None
                or self._guided_window is not None
                or self._parse_review_window is not None
                or self._new_analysis_reset_pending
                or self._institutional_launch_request is not None
            ):
                return
            paper = getattr(self, "_paper_path", "")
            references_only = False
            if paper:
                roles = self._selected_llm_roles()
                jury1_configured = bool(roles.get("jury1"))
                jury2_configured = bool(roles.get("jury2"))
                if jury1_configured != jury2_configured:
                    missing_role = "jury2" if jury1_configured else "jury1"
                    choice = self._confirm_one_jury_run(missing_role)
                    if choice == "choose":
                        self._show_jury_models(missing_role)
                        return
                    if choice != "references_only":
                        return
                    references_only = True
                elif not jury1_configured and not jury2_configured:
                    if not self._confirm_no_jury_run():
                        return
                    references_only = True
            if paper and source_contact_loader is not None:
                settings = QtCore.QSettings("Callimachus", "Callimachus")
                if not settings.value("source_contact_suppressed", False, type=bool):
                    try:
                        contact = source_contact_loader()
                    except (OSError, ValueError, RuntimeError):
                        QtWidgets.QMessageBox.warning(self, text["tab_settings"], text["source_contact_load_failed"])
                        return
                    if not contact.get("has_mail") and not contact.get("has_keys"):
                        from core.gui.settings.services import ask_contact
                        if not ask_contact(self, QtCore, QtWidgets, text, settings_saver):
                            return
            command, run_dir, environment = _command_spec(
                command_builder(paper, self._selected_mode(), self._selected_jury_level())
            )
            if self.skip_parse_review.isChecked():
                command = [item for item in command if item != "--manual-review"]
            elif "--manual-review" not in command:
                command.append("--manual-review")
            selected_preview = (
                {"available": False}
                if references_only else self._selected_llm_preview()
            )
            if (
                not references_only
                and not selected_preview.get("available")
                and any(self._selected_llm_roles().values())
            ):
                self.llm_status.setText(text["jury_readiness_failed"])
                self._show_provider_check_warning(text["jury_readiness_invalid"])
                return
            if selected_preview.get("available"):
                environment = {**(environment or {}), **dict(selected_preview.get("overlay") or {})}
                command = [item for item in command if item != "--references-only"]
            else:
                environment = {
                    **(environment or {}),
                    "CITATION_VERIFIER_VERIFY_BACKENDS": "",
                    "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "",
                    "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "",
                }
                if "--references-only" not in command:
                    command.append("--references-only")
            if not paper or not command:
                self.phase_label.setText(text["phase_waiting"])
                return
            if run_dir:
                self._run_dir = run_dir
                self._display_run_done = False
                self._display_references_only = None
                self._run_environment_by_dir[run_dir] = dict(environment or {})
            self._start_process(
                command, environment=environment, institutional_preflight=True
            )

        def _start_process(
            self,
            command: Sequence[str],
            *,
            environment: Mapping[str, str] | None = None,
            auto_open_report: bool = True,
            institutional_preflight: bool = False,
            readiness_selection: tuple[Mapping[str, Sequence[str]], str] | None = None,
        ):
            if run_environment_defaults is not None:
                from core.app.settings.config import gui_dispatch_defaults
                defaults = gui_dispatch_defaults(run_environment_defaults, environment or {})
                environment = {**(environment or {}), **defaults}
            if (
                self._jury_readiness_request is not None
                or self._institutional_launch_request is not None
            ):
                self._release_child_database_bootstrap()
                return
            if jury_readiness_callback is None or "--references-only" in command:
                return self._after_jury_readiness(
                    command,
                    environment=environment,
                    auto_open_report=auto_open_report,
                    institutional_preflight=institutional_preflight,
                )
            if self._process is not None or self._commands_process is not None or self._new_analysis_reset_pending:
                return
            frozen_selection = readiness_selection is not None
            roles, level = readiness_selection or (
                self._selected_llm_roles(), self._selected_jury_level()
            )
            selection = (
                {role: list(roles.get(role, ())) for role in ("jury1", "jury2")},
                str(level), self._settings_revision, frozen_selection,
            )
            self._jury_readiness_request = (
                list(command), dict(environment or {}), auto_open_report,
                selection, institutional_preflight,
            )
            checking = text["jury_readiness_checking"]
            self.llm_status.setText(checking)
            self.phase_label.setToolTip("")
            self.phase_label.setText(checking)
            self.current_activity.setText(
                text["current_activity"].format(activity=checking)
            )
            self._update_run_controls()
            self._queue_load("jury_readiness", lambda delta=dict(environment or {}): jury_readiness_callback(delta))
            if "jury_readiness" not in self._loading_kinds:
                self._complete_jury_readiness(None, "queue_failed")

        def _complete_jury_readiness(self, payload, error):
            pending = self._jury_readiness_request
            self._jury_readiness_request = None
            self._update_run_controls()
            if pending is None:
                return
            command, environment, auto_open_report, selection, institutional_preflight = pending
            selected_roles, selected_level, selected_revision, frozen_selection = selection
            changed = selected_revision != self._settings_revision
            if not frozen_selection:
                changed = changed or (
                    selected_roles != self._selected_llm_roles()
                    or selected_level != self._selected_jury_level()
                )
            if changed:
                changed = text["jury_readiness_changed"]
                self.llm_status.setText(changed)
                self.phase_label.setText(changed)
                self.current_activity.setText(
                    text["current_activity"].format(activity=changed)
                )
                self._release_child_database_bootstrap()
                return
            if not error and isinstance(payload, Mapping) and payload.get("ok") is True:
                self._after_jury_readiness(
                    command,
                    environment=environment,
                    auto_open_report=auto_open_report,
                    institutional_preflight=institutional_preflight,
                )
                return
            result = payload if not error and isinstance(payload, Mapping) else {}
            message = self._provider_catalog_error(result)
            selector = str(result.get("selector") or "")
            if selector:
                message = f"{selector}: {message}"
            failed = text["jury_readiness_failed"]
            self.llm_status.setText(failed)
            self.phase_label.setText(failed)
            self.current_activity.setText(
                text["current_activity"].format(activity=failed)
            )
            self._show_provider_check_warning(message)
            self._release_child_database_bootstrap()

        def _after_jury_readiness(
            self,
            command: Sequence[str],
            *,
            environment: Mapping[str, str] | None,
            auto_open_report: bool,
            institutional_preflight: bool,
        ):
            if (
                institutional_preflight
                and institutional_access_config_loader is not None
            ):
                self._begin_institutional_preflight(
                    command,
                    environment=environment,
                    auto_open_report=auto_open_report,
                )
                return
            self._launch_process(
                command, environment=environment, auto_open_report=auto_open_report
            )

        def _begin_institutional_preflight(
            self,
            command: Sequence[str],
            *,
            environment: Mapping[str, str] | None,
            auto_open_report: bool,
        ):
            from .institutional_access import prompt_is_suppressed

            settings = QtCore.QSettings("Callimachus", "Callimachus")
            if prompt_is_suppressed(settings):
                config = self._institutional_config()
                return self._launch_process(
                    command,
                    environment={
                        **dict(environment or {}),
                        **self._institutional_environment(config),
                    },
                    auto_open_report=auto_open_report,
                )
            if self._institutional_dialog is not None:
                return
            self._institutional_launch_request = (
                list(command), dict(environment or {}), auto_open_report,
            )
            self._update_run_controls()
            self._update_history_actions()
            self._show_institutional_login()

        def _institutional_config(self) -> dict[str, Any]:
            if institutional_access_config_loader is None:
                return {}
            try:
                value = institutional_access_config_loader()
            except (OSError, RuntimeError, ValueError):
                return {}
            return dict(value) if isinstance(value, Mapping) else {}

        @staticmethod
        def _institutional_environment(config: Mapping[str, Any]) -> dict[str, str]:
            from .institutional_access import institutional_environment_overlay

            return institutional_environment_overlay(config)

        def _show_institutional_login(self):
            if self._institutional_dialog is not None:
                self._institutional_dialog.raise_()
                self._institutional_dialog.activateWindow()
                return
            if self._process is not None or self._commands_process is not None:
                # The subprocess may own this persistent profile. Wait for its
                # existing checkpoint rather than launching a competing Chrome
                # or interrupting acquisition to ask for credentials.
                self._institutional_login_deferred = True
                self.institutional_access_status.setText(text["institutional_login_deferred"])
                return
            self._institutional_login_deferred = False
            guided = self._guided_window
            browser_thread = getattr(guided, "_browser_thread", None) if guided is not None else None
            browser_worker = getattr(guided, "_browser_worker", None) if guided is not None else None
            if (
                browser_worker is not None
                and browser_thread is not None
                and browser_thread.isRunning()
                and not bool(getattr(browser_worker, "_parked", False))
            ):
                self.institutional_access_status.setText(
                    text["institutional_profile_busy"]
                )
                if self._institutional_launch_request is not None:
                    self._finish_institutional_preflight("failed", "")
                return

            from .institutional_access import create_institutional_login_dialog

            dialog = create_institutional_login_dialog(
                self,
                text=text,
                config=self._institutional_config(),
                settings_saver=institutional_settings_saver,
                session_factory=institutional_session_factory,
            )
            self._institutional_dialog = dialog
            dialog.completed.connect(self._institutional_dialog_completed)
            self._update_run_controls()
            dialog.show()

        def _open_deferred_institutional_login(self):
            if (
                self._institutional_login_deferred and self._process is None
                and self._commands_process is None and not self._new_analysis_reset_pending
            ):
                self._show_institutional_login()

        def _institutional_dialog_completed(self, outcome: str, profile: str):
            self._institutional_dialog = None
            if self._institutional_close_pending:
                self._institutional_close_pending = False
                QtCore.QTimer.singleShot(0, self.close)
                return
            if outcome == "prepared" and profile:
                self._institutional_session_prepared = True
                self.institutional_access_status.setText(
                    text["institutional_session_prepared"].format(profile=profile)
                )
                self.institutional_session_ready.emit(profile)
            elif outcome == "failed":
                self.institutional_access_status.setText(
                    text["institutional_access_unavailable"]
                )
            self._finish_institutional_preflight(outcome, profile)
            self._update_run_controls()

        def _finish_institutional_preflight(self, outcome: str, _profile: str):
            pending = self._institutional_launch_request
            self._institutional_launch_request = None
            if pending is None:
                return
            command, environment, auto_open_report = pending
            if outcome in {"continue", "failed"}:
                environment["CALLIMACHUS_INSTITUTIONAL_FETCH"] = "0"
            else:
                config = self._institutional_config()
                if outcome == "prepared":
                    config.update(enabled=True, profile=_profile)
                environment.update(self._institutional_environment(config))
            if self._run_dir:
                self._run_environment_by_dir[self._run_dir] = dict(environment)
            self._launch_process(
                command,
                environment=environment,
                auto_open_report=auto_open_report,
            )
            self._update_history_actions()

        def _queue_institutional_retry(self, profile: str):
            """Renew browser access without writing to an active run database."""
            config = self._institutional_config()
            config.update(enabled=True, profile=profile)
            environment = self._institutional_environment(config)
            environment["CALLIMACHUS_INSTITUTIONAL_RETRY_TOKEN"] = uuid.uuid4().hex
            if self._institutional_launch_request is not None:
                command, pending_environment, auto_open = self._institutional_launch_request
                self._institutional_launch_request = (
                    command, {**pending_environment, **environment}, auto_open,
                )
                return
            if not self._run_dir:
                return
            self._run_environment_by_dir[self._run_dir] = {
                **self._run_environment_by_dir.get(self._run_dir, {}), **environment,
            }
            self._institutional_retry_pending = self._run_dir
            self._retry_institutional_when_idle()

        def _retry_institutional_when_idle(self) -> bool:
            run_dir = self._institutional_retry_pending
            if (
                not run_dir or run_dir != self._run_dir
                or self._process is not None or self._commands_process is not None
                or not self._guided_fetch_available or resume_command_builder is None
                or self._jury_readiness_request is not None
            ):
                return False
            # A single pipeline writer owns admission and integrity. Resume at
            # the Fetch checkpoint, rather than writing from the login thread.
            window = self._guided_window
            if window is not None:
                if (
                    getattr(window, "_proceed_preparing", False)
                    or getattr(window, "_proceed_thread", None) is not None
                ):
                    return False
                busy = getattr(window, "_identity_operation_busy", None)
                if callable(busy) and busy():
                    return False
                source_stage_busy = getattr(window, "_source_stage_busy", None)
                if callable(source_stage_busy) and source_stage_busy():
                    return False
                if getattr(window, "_browser_preflight_request_id", None) is not None:
                    return False
                if not window.close():
                    return False
                self._guided_window = None
            command, _ignored, environment = _command_spec(resume_command_builder(run_dir))
            if not command:
                return False
            self._institutional_retry_pending = None
            self._start_process(
                command, environment=self._resume_environment(run_dir, environment),
            )
            return True

        def _reset_institutional_prompt_preference(self):
            from .institutional_access import reset_prompt_preference

            reset_prompt_preference(QtCore.QSettings("Callimachus", "Callimachus"))
            self.institutional_access_status.setText(
                text["institutional_prompt_reset_done"]
            )

        def _launch_process(
            self,
            command: Sequence[str],
            *,
            environment: Mapping[str, str] | None = None,
            auto_open_report: bool = True,
        ):
            if (
                self._library_busy
                or self._process is not None
                or self._commands_process is not None
                or self._new_analysis_reset_pending
            ):
                self._release_child_database_bootstrap()
                return
            command = self._command_with_manual_policy(command)
            if self._run_dir:
                self._run_environment_by_dir[self._run_dir] = dict(environment or {})
            self._begin_console_capture()
            self._cancel_stop_escalation()
            self._forced_stop_process = None
            self._cooldown_until = None
            self.cooldown_status.hide()
            self._process = QtCore.QProcess(self)
            self._analysis_stdout_decoder = codecs.getincrementaldecoder("utf-8")(
                errors="replace"
            )
            self._analysis_stderr_decoder = codecs.getincrementaldecoder("utf-8")(
                errors="replace"
            )
            self._auto_open_report_on_finish = auto_open_report
            self._process.readyReadStandardOutput.connect(
                lambda: self._read_output(stream="stdout")
            )
            self._process.readyReadStandardError.connect(
                lambda: self._read_output(stream="stderr")
            )
            self._process.finished.connect(self._on_process_finished)
            self._process.errorOccurred.connect(self._on_process_error)
            process_environment = QtCore.QProcessEnvironment.systemEnvironment()
            for name in self._settings_environment_names:
                process_environment.remove(name)
            for name, value in (environment or {}).items():
                process_environment.insert(str(name), str(value))
            from core.fetch.transport.network_consent import CONTROL_ENV
            from .network_permissions import PermissionController
            consent_key = secrets.token_hex(32)
            active_process = self._process
            def send_network_decision(line):
                if (
                    self._process is active_process and not self._stopping
                    and active_process.state() != QtCore.QProcess.ProcessState.NotRunning
                ):
                    active_process.write((line + "\n").encode("utf-8"))
            self._network_consent_controller = PermissionController(
                consent_key, self._get_network_permissions_store(),
                self._ask_network_permission, send_network_decision,
                on_error=self._network_permission_error,
            )
            process_environment.insert(CONTROL_ENV, consent_key)
            process_environment.insert("CALLIMACHUS_DESKTOP_CONTROL", "1")
            process_environment.insert("PYTHONIOENCODING", "utf-8:replace")
            self._process.setProcessEnvironment(process_environment)
            self._stopping = False
            self._last_phase_progress = None
            self._set_error_state(False)
            self.phase_label.setToolTip("")
            self.current_activity.setText(
                text["current_activity"].format(activity=text["running"])
            )
            self.phase_progress.setRange(0, 0)
            self.phase_progress_percent.setText("—")
            self._update_run_controls()
            self.phase_label.setText(text["running"])
            self._poll_timer.start()
            self._refresh_llm_status()
            self._process.start(command[0], list(command[1:]))

        def _command_with_manual_policy(self, command: Sequence[str]) -> list[str]:
            configured = list(command)
            if not self.skip_manual_checks.isChecked() or "--proceed" not in configured:
                return configured
            if "--challenge-mode" in configured:
                index = configured.index("--challenge-mode")
                if index + 1 < len(configured):
                    configured[index + 1] = "off"
            else:
                configured.extend(("--challenge-mode", "off"))
            return configured

        def _on_process_error(self, error):
            if self._process is None:
                return
            message = f"{text['process_error']}: {self._process.errorString()}"
            self.phase_label.setText(message)
            self._show_phase_activity()
            self._append_console_output(f"{message}\n")
            if error == QtCore.QProcess.ProcessError.FailedToStart:
                self._set_error_state(True)
                self._cancel_stop_escalation()
                self._forced_stop_process = None
                self._process = None
                self._stopping = False
                self._poll_timer.stop()
                self._refresh_llm_status()
                self._update_run_controls()
                if self._new_analysis_reset_pending:
                    self._clear_analysis()
                elif self._release_child_database_bootstrap(refresh=False):
                    if self._source_query_active():
                        self._request_source_query()
                    else:
                        self._request_source_page()
                    self._request_snapshot(force=True)

        def _cancel_stop_escalation(self):
            timer = self._stop_escalation_timer
            self._stop_escalation_timer = None
            if timer is not None:
                timer.stop()
                timer.deleteLater()

        def _schedule_stop_escalation(self, process):
            self._cancel_stop_escalation()
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(_STOP_ESCALATION_GRACE_MS)
            timer.timeout.connect(
                lambda process=process, timer=timer: self._escalate_stop(
                    process, timer
                )
            )
            self._stop_escalation_timer = timer
            timer.start()

        def _escalate_stop(self, process, timer):
            if timer is not self._stop_escalation_timer:
                return
            self._stop_escalation_timer = None
            timer.deleteLater()
            if process is not self._process or not self._stopping:
                return
            if process.state() == QtCore.QProcess.ProcessState.NotRunning:
                return
            self._forced_stop_process = process
            process.kill()

        def _advance_spinner(self):
            self._spinner_frame = (self._spinner_frame + 1) % 12
            opacity = 0.35 + 0.5 * (1 + math.sin(time.monotonic() * 4)) / 2
            for effect in self._library_skeleton_effects:
                effect.setOpacity(opacity)
            if self._console_page_loading:
                self.console_history_spinner.setText(
                    "◐◓◑◒"[self._spinner_frame % 4]
                )
            self._update_cooldown_status()
            if hasattr(self,"_atlas_history_spinner") and self._atlas_history_spinner.isVisible():
                self._atlas_history_spinner.setPixmap(_spinner_icon(QtCore,QtGui,self._spinner_frame, color="#7ab4ff" if self._dark else "#175fe6").pixmap(22,22))
                self.history_table.viewport().update()
            if self._page_index == 2 and "cache_page" in self._loading_kinds:
                self.cache_table.viewport().update()
            if self._report_export_spinner is not None:
                color = "#7ab4ff" if self._dark else "#175fe6"
                self._report_export_spinner.setPixmap(
                    _spinner_icon(QtCore, QtGui, self._spinner_frame, color=color).pixmap(24, 24)
                )
            if self._process is not None:
                self.start_button.setIcon(_spinner_icon(QtCore, QtGui, self._spinner_frame))
            if self._background_loading:
                for index in range(self.source_table.rowCount()):
                    source_item = self.source_table.item(index, 0)
                    row = source_item.data(QtCore.Qt.ItemDataRole.UserRole) if source_item else None
                    if isinstance(row, Mapping) and str(row.get("status") or "").casefold() == "in_corso":
                        status_item = self.source_table.item(index, 3)
                        if status_item is not None:
                            status_item.setText(f"{'◐◓◑◒'[self._spinner_frame % 4]} {text['status_running']}")
            elif any(
                str(row.get("status") or "").casefold() == "in_corso"
                for row in self._source_rows
            ):
                self._render_sources()

        def _get_network_permissions_store(self):
            from .network_permissions import PermissionStore
            if self._network_permissions_store is None:
                settings = QtCore.QSettings("Callimachus", "Callimachus")
                def save(value):
                    # QSettings reports its first error; use a fresh instance so
                    # a corrected storage failure can be retried successfully.
                    current_settings = QtCore.QSettings("Callimachus", "Callimachus")
                    current_settings.setValue("network_permissions", value)
                    current_settings.sync()
                    if current_settings.status() != QtCore.QSettings.Status.NoError:
                        raise OSError("network permissions could not be saved")
                self._network_permissions_store = PermissionStore(
                    lambda: settings.value("network_permissions", "[]"), save,
                )
            return self._network_permissions_store

        def _ask_network_permission(self, scope):
            from .network_permissions import ask_permission
            return ask_permission(self, QtWidgets, scope, language=language)

        def _network_permission_error(self, _message):
            QtWidgets.QMessageBox.warning(
                self, "Rete" if language.startswith("it") else "Network",
                "Impossibile salvare o applicare l’autorizzazione. Connessione non consentita."
                if language.startswith("it") else
                "The permission could not be saved or applied. The connection was denied.",
            )

        def _manage_network_permissions(self):
            from .network_permissions import manage_permissions
            try:
                manage_permissions(
                    self, QtWidgets, self._get_network_permissions_store(), language=language,
                )
            except Exception:
                QtWidgets.QMessageBox.warning(
                    self, "Rete" if language.startswith("it") else "Network",
                    "Impossibile salvare le autorizzazioni di rete." if language.startswith("it")
                    else "Network permissions could not be saved.",
                )

        def _read_output(self, *, stream: str | None = None, final: bool = False):
            if self._process is None:
                return
            if not all(callable(getattr(self._process, name, None)) for name in (
                "readAllStandardOutput", "readAllStandardError"
            )):
                return
            # Terminal output is diagnostic only: durable snapshots own phase/progress.
            stdout = (
                bytes(self._process.readAllStandardOutput())
                if stream in (None, "stdout") else b""
            )
            stderr = (
                bytes(self._process.readAllStandardError())
                if stream in (None, "stderr") else b""
            )
            stdout_decoder = self._analysis_stdout_decoder
            stderr_decoder = self._analysis_stderr_decoder
            stdout_text = stdout_decoder.decode(stdout, final=final) if stdout_decoder else ""
            stderr_text = stderr_decoder.decode(stderr, final=final) if stderr_decoder else ""
            controller = self._network_consent_controller
            if controller is not None:
                stderr_text = controller.feed(stderr_text)
                if final:
                    stderr_text += controller.flush()
                    self._network_consent_controller = None
            payload = (stdout_text + stderr_text).encode("utf-8")
            for line in payload.decode("utf-8", errors="replace").splitlines():
                seconds = _cooldown_seconds(line)
                if seconds is not None:
                    self._cooldown_until = time.monotonic() + seconds
                    self._cooldown_activity = _safe_activity_text(line.encode("utf-8"))
            self._update_cooldown_status()
            activity = _safe_activity_text(payload)
            if activity:
                self.current_activity.setText(
                    text["current_activity"].format(activity=activity)
                )
            if final:
                self._analysis_stdout_decoder = None
                self._analysis_stderr_decoder = None
            if stdout_text:
                self._append_console_output(stdout_text)
            if stderr_text:
                self._append_console_output(stderr_text)

        def _update_cooldown_status(self):
            if self._process is None or self._cooldown_until is None:
                self.cooldown_status.hide()
                return
            remaining = math.ceil(self._cooldown_until - time.monotonic())
            if remaining <= 0:
                self._cooldown_until = None
                self.cooldown_status.hide()
                return
            self.cooldown_status.setText(
                text["provider_cooldown"].format(
                    seconds=remaining, activity=self._cooldown_activity
                )
            )
            self.cooldown_status.show()

        def _on_process_finished(self, exit_code: int, _exit_status):
            process = self._process
            if process is not None:
                self._read_output(final=True)
            auto_open_report = self._auto_open_report_on_finish
            self._auto_open_report_on_finish = True
            forced_stop = (
                process is not None and self._forced_stop_process is process
            )
            stopped_identity = self._stopped_session_identity
            self._stopped_session_identity = None
            if (
                stopped_identity is not None and stopped_identity[0] is process
                and process.state() == QtCore.QProcess.ProcessState.NotRunning
                and stop_session_finalize is not None
            ):
                try:
                    stop_session_finalize(stopped_identity[1], stopped_identity[2])
                except Exception as exc:
                    # Leave the durable active guard in place on any failure.
                    self._append_console_output(
                        f"[Stop] Could not reconcile the exited session ({type(exc).__name__}).\n"
                    )
            self._cancel_stop_escalation()
            self._forced_stop_process = None
            self._process = None
            child_bootstrap_finished = self._release_child_database_bootstrap(
                refresh=False
            )
            self._cooldown_until = None
            self.cooldown_status.hide()
            self._poll_timer.stop()
            self._stopping = False
            self._refresh_llm_status()
            if self._new_analysis_reset_pending:
                self._clear_analysis()
                return
            if self._background_loading:
                self._pending_finish = (exit_code, _exit_status, forced_stop, auto_open_report)
                self.phase_label.setText(text["loading_run"])
                self._invalidate_load("snapshot")
                self._snapshot_loading_run = None
                if child_bootstrap_finished:
                    if self._source_query_active():
                        self._request_source_query()
                    else:
                        self._request_source_page()
                self._request_snapshot(force=True)
                self._invalidate_load("latest")
                self._latest_loaded = False
                self._queue_load("latest", latest_loader)
                return
            snapshot = self._refresh_snapshot()
            self._refresh_static_tabs()
            self._finish_process_after_snapshot(
                snapshot, exit_code, _exit_status, forced_stop, auto_open_report
            )

        def _finish_process_after_snapshot(
            self, snapshot, exit_code: int, _exit_status, forced_stop: bool,
            auto_open_report: bool,
        ):
            if self._new_analysis_reset_pending:
                self._clear_analysis()
                return
            self.phase_label.setToolTip("")
            self._set_error_state(False)
            if forced_stop:
                self._set_error_state(False, warning=True)
                self.phase_label.setText(text["process_stopped_unclean"])
            elif _exit_status == QtCore.QProcess.ExitStatus.CrashExit:
                self._set_error_state(True)
                self.phase_label.setText(text["process_crashed"])
                self.phase_label.setToolTip(
                    text["process_exit_detail"].format(exit_code=exit_code)
                )
            elif exit_code == 10:
                if snapshot is None:
                    self._set_error_state(True)
                    self.phase_label.setText(text["snapshot_unavailable"])
                    self._show_phase_activity()
                    self._update_run_controls()
                    return
                if self._guided_fetch_available and self._retry_institutional_when_idle():
                    return
                if self._parse_review_available:
                    self._open_parse_review(self._run_dir)
                elif self.skip_manual_checks.isChecked():
                    if self._skip_pending_manual_tasks(self._run_dir):
                        self._resume_after_manual_skip(self._run_dir)
                elif self._guided_fetch_available:
                    self._open_guided_fetch(self._run_dir)
                elif snapshot.get("identity_review_only") is True:
                    self._resume_after_manual_skip(self._run_dir)
            elif exit_code == 130:
                self.phase_label.setText(text["process_interrupted"])
            elif exit_code != 0:
                self._set_error_state(True)
                message_key = {
                    1: "process_failed_startup",
                    2: "process_failed_pipeline",
                    3: "process_run_locked",
                    20: "process_gate_failed",
                    21: "process_report_untrusted",
                }.get(exit_code, "process_failed")
                self.phase_label.setText(
                    text[message_key].format(exit_code=exit_code)
                )
                self.phase_label.setToolTip(
                    text["process_exit_detail"].format(exit_code=exit_code)
                )
            elif snapshot is not None and snapshot.get("phase") == "done":
                activity_key = (
                    "activity_references_only_done"
                    if snapshot.get("references_only")
                    else "activity_completed"
                )
                self.current_activity.setText(
                    text["current_activity"].format(activity=text[activity_key])
                )
                report_path = self._current_report_path()
                if auto_open_report and report_path:
                    self._open_path(report_path)
            if forced_stop or _exit_status == QtCore.QProcess.ExitStatus.CrashExit or exit_code not in {0, 10}:
                self._show_phase_activity()
            self._update_run_controls()
            if not forced_stop and _exit_status == QtCore.QProcess.ExitStatus.NormalExit and exit_code in {0, 10}:
                self._open_deferred_institutional_login()

        def _show_phase_activity(self):
            self.current_activity.setText(
                text["current_activity"].format(activity=self.phase_label.text())
            )
            if self.phase_label.toolTip():
                self.current_activity.setToolTip(
                    self.current_activity.text() + "\n" + self.phase_label.toolTip()
                )

        def _set_error_state(self, enabled: bool, *, warning: bool = False):
            for widget in (
                self.phase_label, self.phase_progress, self.current_activity,
                self.phase_progress_percent,
            ):
                widget.setProperty("error", enabled)
                widget.setProperty("warning", warning)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()
            if (enabled or warning) and self.phase_progress.maximum() == 0:
                self.phase_progress.setRange(0, 100)
                value = self._last_phase_progress
                self.phase_progress.setValue(value if value is not None else 0)
                self.phase_progress_percent.setText(
                    f"{value}%" if value is not None else "—"
                )

        def _refresh_snapshot(self):
            if not self._run_dir:
                return None
            try:
                snapshot = dict(snapshot_loader(self._run_dir) or {})
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
                return None
            return self._apply_snapshot(snapshot)

        def _apply_snapshot(self, snapshot: Mapping[str, Any]):
            self._apply_institutional_snapshot(snapshot.get("institutional_access"))
            self._display_run_done = bool(
                snapshot.get("phase") == "done" and snapshot.get("run_status") == "completed"
            )
            self._display_references_only = snapshot.get("references_only") if isinstance(
                snapshot.get("references_only"), bool
            ) else None
            self._guided_fetch_available = bool(
                snapshot.get("phase") == "fetch"
                and (
                    snapshot.get("fetch_paused")
                    or (snapshot.get("paused") and snapshot.get("action_required"))
                )
            )
            self._parse_review_pending = int(snapshot.get("parse_review_pending") or 0)
            self._parse_review_available = bool(
                snapshot.get("phase") == "parse"
                and (
                    snapshot.get("paused")
                    or snapshot.get("run_status") == "paused"
                )
            )
            phase = snapshot.get("phase")
            self._atlas_current_phase = phase
            if isinstance(phase, str) and phase:
                phase_text = {
                    "parse": text["phase_parse"],
                    "resolve": text["phase_resolve"],
                    "fetch": text["phase_fetch"],
                    "gaps": text["phase_verify"],
                    "style": text["phase_verify"],
                    "verify": text["phase_verify"],
                    "web_research": text["phase_verify"],
                    "report": text["phase_report"],
                }.get(phase, phase.replace("_", " ").title())
                self.phase_label.setText(phase_text)
            progress = snapshot.get("phase_progress")
            if not isinstance(progress, int) or isinstance(progress, bool):
                progress = None
                counts = snapshot.get("progress_counts")
                unit_keys = {
                    "resolve": ("resolve_completed", "sources_total"),
                    "fetch": ("fetch_completed", "sources_total"),
                    "gaps": ("verify_completed", "verify_total"),
                    "style": ("verify_completed", "verify_total"),
                    "verify": ("verify_completed", "verify_total"),
                    "web_research": ("verify_completed", "verify_total"),
                }.get(phase)
                if phase == "done" and snapshot.get("run_status") == "completed":
                    progress = 100
                elif isinstance(counts, Mapping) and unit_keys is not None:
                    completed = counts.get(unit_keys[0])
                    total = counts.get(unit_keys[1])
                    if (
                        isinstance(completed, int)
                        and not isinstance(completed, bool)
                        and isinstance(total, int)
                        and not isinstance(total, bool)
                        and total > 0
                    ):
                        progress = round(max(0, min(total, completed)) * 100 / total)

            if progress is not None:
                self._last_phase_progress = max(0, min(100, progress))
                self.phase_progress.setRange(0, 100)
                self.phase_progress.setValue(self._last_phase_progress)
                self.phase_progress_percent.setText(f"{self._last_phase_progress}%")
                counts = snapshot.get("progress_counts")
                unit_keys = {
                    "resolve": ("resolve_completed", "sources_total"),
                    "fetch": ("fetch_completed", "sources_total"),
                    "gaps": ("verify_completed", "verify_total"),
                    "style": ("verify_completed", "verify_total"),
                    "verify": ("verify_completed", "verify_total"),
                    "web_research": ("verify_completed", "verify_total"),
                }.get(phase)
                if isinstance(counts, Mapping) and unit_keys is not None:
                    completed = counts.get(unit_keys[0])
                    total = counts.get(unit_keys[1])
                    if (
                        isinstance(completed, int)
                        and not isinstance(completed, bool)
                        and isinstance(total, int)
                        and not isinstance(total, bool)
                        and total > 0
                    ):
                        self.phase_progress.setFormat(
                            text["phase_progress_units"].format(
                                phase=phase_text,
                                completed=max(0, min(total, completed)),
                                total=total,
                                percent="%p%",
                            )
                        )
                    else:
                        self.phase_progress.setFormat(text["phase_progress"])
                else:
                    self.phase_progress.setFormat(text["phase_progress"])
            else:
                self.phase_progress.setRange(0, 0)
                self.phase_progress.setFormat(text["phase_progress"])
                self.phase_progress_percent.setText("—")
            if phase == "done" and snapshot.get("references_only"):
                self.phase_label.setText(text["phase_references_only_done"])
                self.phase_progress.setFormat(text["progress_references_only_done"])
            refs = snapshot.get("references") or snapshot.get("source_inventory") or []
            if self._source_page_mode:
                self._update_run_controls()
                return snapshot
            source_rows = [row for row in refs if isinstance(row, Mapping)]
            rows_changed = source_rows != self._source_rows
            if source_rows:
                self._source_rows = source_rows
                if self._pending_child_source_dir == self._run_dir:
                    self._pending_child_source_dir = None
            elif self._pending_child_source_dir != self._run_dir or phase in {"done", "failed"}:
                self._source_rows = []
                self._source_visible_count = 0
                self._pending_child_source_dir = None
            if (not self._background_loading or self._page_index == 0) and rows_changed:
                self._render_sources()
            self._update_run_controls()
            return snapshot

        def _latest_resumable_row(self):
            try:
                rows = list(history_loader() or ())
            except (OSError, ValueError):
                return None
            return next((row for row in rows if isinstance(row, Mapping) and self._history_row_is_resumable(row)), None)

        def _latest_resume_state(self):
            if self._background_loading:
                rows = [self._latest_run_row] if isinstance(self._latest_run_row, Mapping) else []
                if self._latest_load_error:
                    return None, text["load_failed"].format(reason=self._latest_load_error)
                if not rows and "latest" in self._loading_kinds:
                    return None, text["loading_run"]
            else:
                try:
                    rows = [row for row in (history_loader() or ()) if isinstance(row, Mapping)]
                except (OSError, ValueError):
                    return None, text["resume_latest_unavailable"]
            candidate = rows[0] if rows else None
            if candidate is not None and candidate.get("latest_lookup_pending"):
                return candidate, text["atlas_find_latest"]
            if candidate is not None and self._history_row_is_resumable(candidate):
                label = str(candidate.get("paper") or candidate.get("input") or candidate.get("run_dir") or text["unavailable"])
                if candidate.get("crash_recoverable"):
                    return candidate, text["resume_latest_crash"].format(paper=label)
                return candidate, text["resume_latest_target"].format(paper=label)
            if not rows:
                return None, text["resume_latest_no_history"]
            latest = rows[0]
            if latest.get("available", True) is not True:
                return None, text["resume_latest_unavailable"]
            if latest.get("run_status") == "active":
                return None, text["resume_latest_active"]
            return None, text["resume_latest_completed"]

        def _update_run_controls(self):
            active = self._process is not None
            commands_active = self._commands_process is not None
            review_open = self._guided_window is not None or self._parse_review_window is not None
            reset_pending = self._new_analysis_reset_pending
            has_analysis_state = bool(
                self._run_dir or getattr(self, "_paper_path", "")
            )
            self.start_button.setEnabled(
                not active and not review_open and not commands_active and not reset_pending
                and self._jury_readiness_request is None
                and self._institutional_launch_request is None
            )
            self.institutional_login_button.setEnabled(
                self._institutional_dialog is None and not self._load_closed
            )
            self.skip_manual_checks.setEnabled(
                not active and not review_open and not commands_active and not reset_pending
                and self._institutional_launch_request is None
            )
            self.skip_parse_review.setEnabled(
                not active and not review_open and not commands_active and not reset_pending
                and self._institutional_launch_request is None
            )
            commands_button = getattr(self, "commands_run_button", None)
            if commands_button is not None:
                commands_button.setEnabled(
                    not active and not review_open and not commands_active and not reset_pending
                    and self._institutional_launch_request is None
                )
            new_analysis_tip = text[
                "new_analysis_pending_tip" if reset_pending or self._stopping
                else "new_analysis_stop_tip" if active
                else "new_analysis_tip"
            ]
            new_analysis_label = text["new_analysis_stop" if active else "new_analysis"]
            self.new_analysis_button.setText(new_analysis_label)
            self.new_analysis_button.setAccessibleName(new_analysis_label)
            self.new_analysis_button.setToolTip(new_analysis_tip)
            self.new_analysis_button.setAccessibleDescription(new_analysis_tip)
            self.new_analysis_button.setEnabled(
                (active or has_analysis_state)
                and not review_open and not commands_active and not self._stopping
                and not reset_pending
                and self._institutional_launch_request is None
            )
            self.start_button.setText("" if active else text["start"])
            self.start_button.setIcon(_spinner_icon(QtCore, QtGui, self._spinner_frame) if active else QtGui.QIcon())
            if active:
                self.resume_latest_button.setObjectName("runStopButton")
                self.resume_latest_button.setText(text["stopping"] if self._stopping else text["stop"])
                self.resume_latest_button.setEnabled(not self._stopping)
            else:
                self.resume_latest_button.setObjectName("runResumeButton")
                self.resume_latest_button.setText(text["atlas_finding_latest"] if "latest_resume" in self._loading_kinds else text["resume_latest"])
                row, tooltip = self._latest_resume_state()
                self.resume_latest_button.setToolTip(tooltip)
                self.resume_latest_button.setEnabled(
                    not review_open and not commands_active and not reset_pending
                    and self._institutional_launch_request is None
                    and row is not None and "latest_resume" not in self._loading_kinds
                )
            self.resume_latest_button.style().unpolish(self.resume_latest_button)
            self.resume_latest_button.style().polish(self.resume_latest_button)
            self._balance_run_button_geometry()
            self.open_guided_fetch_button.setVisible(
                bool(
                    self._run_dir and self._guided_fetch_available
                    and not active and not review_open and not commands_active
                    and self._institutional_launch_request is None
                )
            )
            self.open_parse_review_button.setVisible(
                bool(
                    self._run_dir and self._parse_review_available
                    and not active and not commands_active
                    and self._guided_window is None
                    and self._institutional_launch_request is None
                    and (
                        self._parse_review_window is None
                        or (
                            self._parse_review_window.isHidden()
                            and self._parse_review_run_dir == str(self._run_dir)
                        )
                    )
                )
            )
            self.open_parse_review_button.setText(
                text["open_parse_review_count"].format(count=self._parse_review_pending)
                if self._parse_review_pending else text["resume"]
            )
            report_visible = bool(
                self._current_report_path()
                and not active and not review_open and not commands_active
            )
            if report_visible:
                self.open_guided_fetch_button.hide()
            self.open_run_report_button.setVisible(report_visible)
            self.export_run_report_button.setVisible(report_visible)
            self.export_run_report_button.setEnabled(
                not active and not review_open and not commands_active
                and self._current_run_exportable()
            )
            from .atlante import update_analysis
            update_analysis(self, text)
            from .atlante import refresh_source_filters
            refresh_source_filters(self, QtWidgets, text)

        def _current_run_exportable(self) -> bool:
            report_path = self._current_report_path()
            return bool(
                export_report_callback is not None
                and self._run_dir
                and self._display_run_done
                and self._display_references_only is False
                and report_path
                and Path(report_path).name == "report.html"
            )

        def _current_report_path(self) -> str | None:
            candidates = (
                [self._latest_run_row, *self._history_rows]
                if self._background_loading else self._history_rows
            )
            for row in candidates:
                if not isinstance(row, Mapping) or row.get("run_dir") != self._run_dir:
                    continue
                path = row.get("report_html_path")
                if (
                    row.get("phase") == "done"
                    and row.get("run_status") == "completed"
                    and row.get("report_html_present")
                    and isinstance(path, str) and path
                ):
                    return path
            if self._background_loading and self._display_run_done and self._run_dir:
                for filename in ("report.html", "report.preview.html"):
                    candidate = Path(self._run_dir) / filename
                    if candidate.is_file():
                        return str(candidate)
            return None

        def _open_current_report(self):
            report_path = self._current_report_path()
            if report_path:
                self._open_path(report_path)

        def _export_current_report(self):
            if self._current_run_exportable() and self._run_dir:
                self._export_report_for_run(self._run_dir)

        def _export_report_for_run(self, run_dir: str, *, historical: bool = False):
            if export_report_callback is None or self._report_export_cancel_event is not None:
                return
            default_path = str(Path(run_dir).parent / f"{Path(run_dir).name}.public.html")
            title = (
                text["export_historical_report"] if historical
                else text["export_report"]
            )
            output, _filter = QtWidgets.QFileDialog.getSaveFileName(
                self, title, default_path, "HTML (*.html)"
            )
            if not output:
                return
            self.statusBar().clearMessage()
            cancel_event = threading.Event()
            self._report_export_cancel_event = cancel_event
            self._report_export_close_pending = False
            self._show_report_export_progress(title=title)

            from core.report.human.public_export import PublicExportCancelled

            def export():
                try:
                    return {
                        "path": export_report_callback(
                            run_dir, output, cancel_check=cancel_event.is_set,
                            overwrite=True, historical=historical,
                        ),
                        "historical": historical,
                    }
                except PublicExportCancelled:
                    return {"cancelled": True}

            self._queue_load(
                "report_export",
                export,
            )

        def _show_report_export_progress(self, *, title: str | None = None):
            dialog = ReportExportProgressDialog(self, self._cancel_report_export)
            dialog.setObjectName("reportExportProgressDialog")
            dialog.setWindowTitle(title or text["export_report"])
            dialog.setWindowFlag(QtCore.Qt.WindowType.WindowCloseButtonHint, True)
            dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
            dialog.setStyleSheet(self.styleSheet())
            dialog.setMinimumWidth(360)

            layout = QtWidgets.QVBoxLayout(dialog)
            row = QtWidgets.QHBoxLayout()
            spinner = QtWidgets.QLabel(dialog)
            spinner.setObjectName("reportExportProgressSpinner")
            spinner.setFixedSize(24, 24)
            message = QtWidgets.QLabel(text["export_report_progress"], dialog)
            message.setObjectName("reportExportProgressMessage")
            message.setWordWrap(True)
            row.addWidget(spinner)
            row.addWidget(message, 1)
            layout.addLayout(row)

            buttons = QtWidgets.QHBoxLayout()
            buttons.addStretch(1)
            cancel_button = QtWidgets.QPushButton(text["export_report_cancel"], dialog)
            cancel_button.setObjectName("dangerButton")
            cancel_button.clicked.connect(self._cancel_report_export)
            buttons.addWidget(cancel_button)
            layout.addLayout(buttons)

            self._report_export_dialog = dialog
            self._report_export_spinner = spinner
            self._report_export_message = message
            self._report_export_cancel_button = cancel_button
            color = "#7ab4ff" if self._dark else "#175fe6"
            spinner.setPixmap(
                _spinner_icon(QtCore, QtGui, self._spinner_frame, color=color).pixmap(24, 24)
            )
            dialog.show()

        def _cancel_report_export(self):
            cancel_event = self._report_export_cancel_event
            if cancel_event is None:
                return
            future = self._load_futures.get("report_export")
            if future is not None and future.cancel():
                # A queued export has not entered its callback, so it cannot
                # emit the normal worker completion signal. Invalidate its
                # token and finish the dialog here; running exports keep using
                # the cooperative event above.
                self._load_tokens["report_export"] = (
                    self._load_tokens.get("report_export", 0) + 1
                )
                self._loading_kinds.discard("report_export")
                self._load_futures.pop("report_export", None)
                self._report_export_completed({"cancelled": True}, None)
                return
            if future is not None and future.done():
                return
            cancel_event.set()
            if self._report_export_message is not None:
                self._report_export_message.setText(text["export_report_cancelling"])
            if self._report_export_cancel_button is not None:
                self._report_export_cancel_button.setEnabled(False)

        def _report_export_completed(self, path: Any, error: Any):
            cancelled = isinstance(path, Mapping) and path.get("cancelled") is True
            exported_path = path.get("path") if isinstance(path, Mapping) else path
            dialog = self._report_export_dialog
            if dialog is not None:
                dialog.accept()
                dialog.deleteLater()
            self._report_export_dialog = None
            self._report_export_spinner = None
            self._report_export_message = None
            self._report_export_cancel_button = None
            self._report_export_cancel_event = None
            close_pending = self._report_export_close_pending
            self._report_export_close_pending = False
            if close_pending and (cancelled or not error):
                QtCore.QTimer.singleShot(0, self.close)
                return
            if cancelled:
                self.statusBar().showMessage(text["export_report_cancelled"], 8000)
                return
            if error:
                QtWidgets.QMessageBox.warning(
                    self, text["export_report"],
                    text["export_report_failed"].format(reason=error),
                )
                return
            historical = isinstance(path, Mapping) and path.get("historical") is True
            title = text["export_historical_report"] if historical else text["export_report"]
            from .shell.messages import compact_message_dialog
            box = compact_message_dialog(
                self, QtCore, QtGui, QtWidgets, title=title,
                message=text["export_historical_report_saved"] if historical else text["export_report_saved"],
                path=str(exported_path), dark=self._dark,
                confirm_label=text["ok_action"] if "ok_action" in text else "OK",
                object_name="reportExportSuccessDialog", icon_name="check",
            )
            box.exec()

        def _open_path(self, path: str):
            try:
                opened = open_path_callback(path)
            except (OSError, RuntimeError, ValueError) as exc:
                reason = str(exc)
            else:
                if opened is not False:
                    return
                reason = text["open_path_unavailable"]
            QtWidgets.QMessageBox.warning(
                self, text["open_report"],
                text["open_path_failed"].format(path=path, reason=reason),
            )

        def _skip_pending_manual_tasks(self, run_dir: str | None) -> bool:
            try:
                if not run_dir or skip_manual_callback is None:
                    raise ValueError("automatic task handling is unavailable")
                if ocr_pending_count_loader is not None:
                    ocr_count = ocr_pending_count_loader(run_dir)
                    if not isinstance(ocr_count, int) or isinstance(ocr_count, bool) or ocr_count < 0:
                        raise ValueError("pending OCR count is unavailable")
                    if ocr_count and (
                        QtWidgets.QMessageBox.question(
                            self,
                            text["skip_ocr_title"],
                            text["skip_ocr_question"].format(count=ocr_count),
                        ) != QtWidgets.QMessageBox.StandardButton.Yes
                    ):
                        return False
                count = skip_manual_callback(run_dir)
                if not isinstance(count, int) or count <= 0:
                    raise ValueError("no supported pending manual task was found")
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
                self._set_error_state(True)
                self.phase_label.setText(text["skip_manual_failed"].format(reason=exc))
                self._show_phase_activity()
                return False
            return True

        def _resume_environment(
            self, run_dir: str, environment: Mapping[str, str],
        ) -> dict[str, str]:
            values = {
                **self._run_environment_by_dir.get(run_dir, {}),
                **environment,
            }
            level_key = "CITATION_VERIFIER_VERIFY_JURY2_LEVEL"
            if level_key not in values:
                # A reopened window has no per-run process cache. Use its
                # explicit selection, including disabled Jury2, for the launch.
                selected = self._selected_llm_preview().get("jury2_level")
                if selected is None:
                    selected = self._selected_jury_level()
                values[level_key] = str(selected)
            return values

        def _resume_after_manual_skip(self, run_dir: str | None):
            if not run_dir:
                return
            try:
                command, _child_dir, environment = _command_spec(
                    resume_command_builder(run_dir)
                )
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
                self._set_error_state(True)
                self.phase_label.setText(text["skip_manual_failed"].format(reason=exc))
                self._show_phase_activity()
                return
            if command:
                self._start_process(
                    command,
                    environment=self._resume_environment(run_dir, environment),
                )

        def _balance_run_button_geometry(self):
            buttons = (self.start_button, self.resume_latest_button)
            for button in buttons:
                button.setMinimumSize(0, 0)
                button.ensurePolished()
            width = max(button.sizeHint().width() for button in buttons)
            height = max(54, *(button.sizeHint().height() for button in buttons))
            for button in buttons:
                button.setMinimumWidth(width)
                button.setMinimumHeight(height)
                button.updateGeometry()

        def _resume_latest_or_stop(self):
            if self._commands_process is not None or self._guided_window is not None:
                return
            if self._process is not None:
                self._request_process_stop()
                return
            row, _tooltip = self._latest_resume_state()
            if isinstance(row, Mapping) and row.get("latest_lookup_pending") and latest_resume_loader is not None:
                if "latest_resume" not in self._loading_kinds:
                    self._atlas_latest_resume_context = (self._run_dir, getattr(self, "_paper_path", ""))
                    self._queue_load("latest_resume", latest_resume_loader)
                    self._update_run_controls()
                return
            if not isinstance(row, Mapping):
                return
            run_dir = row.get("run_dir")
            if not isinstance(run_dir, str):
                return
            self._resume_run_row(row)

        def _request_process_stop(self) -> bool:
            if (
                self._process is None
                or self._commands_process is not None
                or self._stopping
            ):
                return False
            process = self._process
            self._stopped_session_identity = None
            if stop_session_capture is not None and self._run_dir:
                try:
                    identity = stop_session_capture(self._run_dir, process.processId())
                    if identity is not None:
                        self._stopped_session_identity = (process, self._run_dir, identity)
                except Exception as exc:
                    # Failure to prove ownership must never authorize deletion.
                    self._append_console_output(
                        f"[Stop] Could not capture the running session ({type(exc).__name__}).\n"
                    )
            self._stopping = True
            process.write(b"STOP\n")
            self._schedule_stop_escalation(process)
            self._update_run_controls()
            return True

        def _resume_run_row(self, row: Mapping[str, Any]):
            if (
                self._commands_process is not None
                or self._guided_window is not None
                or self._new_analysis_reset_pending
                or self._institutional_launch_request is not None
            ):
                return
            if self._parse_review_window is not None:
                self._parse_review_window.raise_()
                self._parse_review_window.activateWindow()
                return
            run_dir = row.get("run_dir")
            if not isinstance(run_dir, str) or not run_dir:
                return
            try:
                command, child_dir, environment = _command_spec(
                    resume_command_builder(run_dir)
                )
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
                QtWidgets.QMessageBox.warning(self, text["tab_history"], str(exc))
                return
            if not command:
                return
            target_dir = child_dir or run_dir
            environment = self._resume_environment(target_dir, environment)
            if resume_callback is not None:
                resume_callback(row)
            if environment:
                self._run_environment_by_dir[target_dir] = dict(environment)
            if target_dir != run_dir:
                # Keep the parent's durable source labels visible while the
                # new child database is being created by the subprocess.
                self.set_run_dir(run_dir)
            self._pending_child_source_dir = target_dir if target_dir != run_dir else None
            self.set_run_dir(target_dir)
            self.navigation.setCurrentRow(0)
            if target_dir == run_dir and self._history_row_has_parse_review(row):
                self._open_parse_review(run_dir)
                return
            if target_dir == run_dir and self._guided_fetch_available and not self.skip_manual_checks.isChecked():
                self._open_guided_fetch(run_dir)
                return
            skip_pending = self.skip_manual_checks.isChecked() and (
                self._guided_fetch_available
                or (row.get("run_status") == "paused" and row.get("action_required"))
            )
            if target_dir == run_dir and skip_pending and not self._skip_pending_manual_tasks(run_dir):
                return
            self._start_process(command, environment=environment)

        def _set_llm_preflight_status(self, state: str, *, providers: int = 0, models: int = 0):
            self._llm_preflight_status = state
            label = getattr(self, "llm_discovery_status", None)
            if label is not None:
                if state == "checking":
                    label.setText(text["llm_discovery_checking"])
                elif state == "ready":
                    label.setText(text["llm_discovery_ready"].format(
                        providers=providers, models=models,
                    ))
                elif state == "changed":
                    label.setText(text["llm_discovery_changed"])
                elif state == "error":
                    label.setText(text["llm_discovery_failed"])
                else:
                    label.setText(text["llm_discovery_idle"])
                color = (
                    "#63d98a" if self._dark else "#187343"
                ) if state == "ready" else (
                    "#ff746c" if self._dark else "#b42318"
                ) if state == "error" else ""
                label.setStyleSheet(f"color: {color};" if color else "")
            button = getattr(self, "llm_preflight_button", None)
            if button is not None:
                button.setEnabled(state != "checking" and not self._load_closed)

        def _start_llm_preflight(self):
            if (
                llm_preflight_loader is None or self._load_closed
                or self._llm_preflight_busy
            ):
                return
            self._llm_preflight_busy = True
            self._llm_preflight_generation_snapshot = dict(
                self._provider_check_generations
            )
            self._set_llm_preflight_status("checking")
            self._queue_load("llm_preflight", llm_preflight_loader)
            if "llm_preflight" not in self._loading_kinds:
                self._llm_preflight_busy = False
                self._set_llm_preflight_status("error")

        def _provider_catalog_error(self, catalog: Mapping[str, Any]) -> str:
            try:
                status_code = int(catalog.get("status_code"))
            except (TypeError, ValueError):
                status_code = None
            if status_code == 401:
                return text["provider_check_401"]
            if status_code == 403:
                return text["provider_check_403"]
            message = str(catalog.get("message") or "").strip()
            return message or text["provider_check_error"]

        def _llm_preflight_provider_summary(self, result: Mapping[str, Any]) -> str:
            provider_id = str(result.get("provider") or "")
            provider_name = self._llm_provider_name(provider_id)
            message = str(result.get("message") or "").strip()
            models = [str(model) for model in result.get("models", ())
                      if isinstance(model, str) and model]
            if result.get("ok") is not True:
                if result.get("status_code") in {401, 403}:
                    message = self._provider_catalog_error(result)
                elif result.get("installed") is False and provider_id not in {"ollama", "freetoken"}:
                    message = text["llm_discovery_not_installed"]
                elif result.get("authenticated") is False:
                    message = text["llm_discovery_not_signed_in"]
                else:
                    message = self._provider_catalog_error(result)
                return text["llm_discovery_provider_error"].format(provider=provider_name, message=message)
            if result.get("installed") is True and not result.get("options"):
                return f"{provider_name}: {text['llm_discovery_not_routable']}"
            if models:
                shown = models[:3]
                if len(models) > len(shown):
                    shown.append(text["llm_discovery_more_models"].format(count=len(models) - len(shown)))
                summary = text["llm_discovery_provider_models"].format(count=len(models), models=", ".join(shown))
                return f"{provider_name}: {summary}"
            return f"{provider_name}: {message or text['llm_discovery_provider_no_models']}"

        def _llm_provider_name(self, provider_id: str) -> str:
            return {
                "claude_cli": "Claude Code", "codex_cli": "Codex",
                "openai_compatible": "OpenAI compatible",
                "gemini_cli": "Gemini CLI", "antigravity_cli": "Antigravity CLI",
                "opencode_cli": "OpenCode CLI", "cursor_cli": "Cursor CLI",
                "copilot_cli": "GitHub Copilot CLI",
                "antigravity": "Antigravity", "ollama": "Ollama", "ollama_local": "Ollama",
                "freetoken": "FreeToken", "freetoken_authenticated": "FreeToken", "openai": "OpenAI",
                "anthropic": "Anthropic", "gemini": "Gemini",
                "openrouter": "OpenRouter", "typesafe": "TypeSafe",
                "xai": "Grok (xAI)",
                "glm": "GLM", "mistral": "Mistral", "opencode": "OpenCode",
            }.get(provider_id, provider_id)

        def _provider_status_text(self, provider_id: str) -> str:
            state = self._provider_check_state.get(provider_id) or {}
            status = state.get("status") or "idle"
            if status == "checking":
                return text["provider_check_checking"]
            if status == "success":
                if provider_id == "host":
                    return text["provider_check_host_configured"]
                models = state.get("models") or []
                if models:
                    shown = [str(model) for model in models[:4]]
                    if len(models) > len(shown):
                        shown.append(text["llm_discovery_more_models"].format(
                            count=len(models) - len(shown),
                        ))
                    return text["provider_check_models"].format(
                        count=len(models), models=", ".join(shown),
                    )
                return text["provider_check_no_models"]
            if status == "error":
                return str(state.get("message") or text["provider_check_error"])
            if status == "stale":
                return text["provider_check_changed"]
            return text["provider_check_idle"]

        def _update_provider_check_controls(self, provider_id: str):
            self._refresh_jury_picker_check()
            controls = self._provider_check_controls.get(provider_id)
            if controls is None:
                return
            button, label = controls
            state = self._provider_check_state.get(provider_id) or {}
            status = str(state.get("status") or "idle")
            button.setEnabled(
                provider_check_callback is not None
                and status != "checking" and not self._load_closed
            )
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setText(self._provider_status_text(provider_id))
            color = (
                "#63d98a" if self._dark else "#187343"
            ) if status == "success" else (
                "#ff746c" if self._dark else "#b42318"
            ) if status in {"error", "stale"} else ""
            label.setStyleSheet(f"color: {color};" if color else "")
            label.setProperty("providerCheckState", status)
            if status == "success":
                foreground, background, hover = (
                    ("#a5f3c5", "#174d35", "#206744") if self._dark
                    else ("#14532d", "#dcfce7", "#bbf7d0")
                )
                button.setStyleSheet(
                    "QPushButton {"
                    f"color: {foreground}; background-color: {background}; "
                    f"border: 1px solid {color}; border-radius: 5px; padding: 4px 9px;"
                    "} QPushButton:hover {"
                    f"background-color: {hover};"
                    "}"
                )
            elif status in {"error", "stale"}:
                foreground, background, hover = (
                    ("#ffd0cc", "#641d1d", "#7d2525") if self._dark
                    else ("#8a1c13", "#fee4e2", "#fecaca")
                )
                button.setStyleSheet(
                    "QPushButton {"
                    f"color: {foreground}; background-color: {background}; "
                    f"border: 1px solid {color}; border-radius: 5px; padding: 4px 9px;"
                    "} QPushButton:hover {"
                    f"background-color: {hover};"
                    "}"
                )
            else:
                button.setStyleSheet("")
            models = state.get("models") or []
            label.setToolTip(", ".join(str(model) for model in models))

        def _add_provider_check_control(self, form, parent, provider_id: str):
            if provider_id in self._provider_check_controls:
                return
            row = QtWidgets.QWidget(parent)
            layout = QtWidgets.QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            button = QtWidgets.QPushButton(text["provider_check_button"], row)
            button.setObjectName(f"providerCheckButton_{provider_id}")
            button.clicked.connect(
                lambda _checked=False, provider=provider_id:
                    self._start_provider_check(provider)
            )
            label = QtWidgets.QLabel(row)
            label.setObjectName(f"providerCheckStatus_{provider_id}")
            label.setWordWrap(True)
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            layout.addWidget(button)
            layout.addWidget(label, 1)
            self._provider_check_controls[provider_id] = (button, label)
            self._update_provider_check_controls(provider_id)
            form.addRow("", row)

        def _provider_check_updates(self, provider_id: str) -> dict[str, str]:
            relevant_names = {
                str(row.get("name") or "") for row in self._settings_rows
                if str(row.get("check_provider") or "") == provider_id
                and row.get("name")
            }
            updates: dict[str, str] = {}
            for name in relevant_names:
                if name not in self._settings_dirty:
                    continue
                value = str(self._settings_dirty[name])
                row = next((item for item in self._settings_rows
                            if str(item.get("name") or "") == name), {})
                if row.get("control") == "secret" and value and set(value) <= {"•", "*"}:
                    continue
                updates[name] = value
            return updates

        def _provider_check_inputs_snapshot(
            self, provider_id: str,
        ) -> tuple[tuple[str, str], ...]:
            endpoint_suffixes = (
                "_BASE_URL", "_BASE", "_URL", "_HOST", "_ENDPOINT", "_URI",
            )
            inputs = {}
            for row in self._settings_rows:
                name = str(row.get("name") or "")
                if str(row.get("check_provider") or "") != provider_id:
                    continue
                if row.get("control") != "secret" and not _is_secret_setting_name(name) \
                        and name not in {"CITATION_VERIFIER_LLM_HOST_COMMAND", "CITATION_VERIFIER_MODEL"} \
                        and not name.upper().endswith(endpoint_suffixes):
                    continue
                if name in self._settings_dirty:
                    value = str(self._settings_dirty[name])
                    if row.get("control") == "secret" and value and set(value) <= {"•", "*"}:
                        continue
                    inputs[name] = value
            return tuple(sorted(inputs.items()))

        def _start_provider_check(self, provider_id: str):
            if (
                provider_check_callback is None or self._load_closed
                or (self._provider_check_state.get(provider_id) or {}).get("status") == "checking"
            ):
                return
            updates = self._provider_check_updates(provider_id)
            snapshot = self._provider_check_inputs_snapshot(provider_id)
            generation = self._provider_check_generations.get(provider_id, 0) + 1
            self._provider_check_generations[provider_id] = generation
            self._provider_check_requests[provider_id] = (
                generation, snapshot, dict(updates),
            )
            self._provider_check_state[provider_id] = {
                "status": "checking", "source": "check",
            }
            self._update_provider_check_controls(provider_id)
            kind = f"provider_check:{provider_id}"
            self._queue_load(
                kind,
                lambda provider=provider_id, delta=dict(updates):
                    provider_check_callback(provider, delta),
            )
            if kind not in self._loading_kinds:
                self._provider_check_state[provider_id] = {
                    "status": "error", "message": text["provider_check_error"],
                }
                self._provider_check_requests.pop(provider_id, None)
                self._update_provider_check_controls(provider_id)

        def _apply_llm_preflight(self, payload: Any, error: Any):
            self._llm_preflight_busy = False
            if error or not isinstance(payload, Mapping):
                self._set_llm_preflight_status("error")
                return
            results = payload.get("providers")
            if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
                self._set_llm_preflight_status("error")
                return
            successful_providers = 0
            model_count = 0
            detail_lines = []
            detail_tooltip_lines = []
            for result in results:
                if not isinstance(result, Mapping):
                    continue
                provider_id = str(result.get("provider") or "").strip()
                if not provider_id:
                    continue
                detail = self._llm_preflight_provider_summary(result)
                detail_lines.append(detail)
                detail_tooltip_lines.append(
                    f"{provider_id}: " + ", ".join(
                        str(model) for model in result.get("models", ())
                        if isinstance(model, str) and model
                    ) if result.get("models") else detail
                )
                if self._provider_check_generations.get(provider_id, 0) != (
                    self._llm_preflight_generation_snapshot.get(provider_id, 0)
                ):
                    continue
                current = self._provider_check_state.get(provider_id) or {}
                if current.get("status") == "checking" or (
                    current.get("source") == "check" and current.get("status") == "success"
                ):
                    if current.get("status") == "success":
                        successful_providers += 1
                        model_count += len(current.get("models") or ())
                    continue
                if result.get("ok") is True:
                    self._record_provider_catalog(provider_id, result, source="startup")
                    successful_providers += 1
                    model_count += len(self._provider_check_state[provider_id]["models"])
                else:
                    self._llm_catalogs.pop(provider_id, None)
                    self._provider_check_state[provider_id] = {
                        "status": "error", "source": "startup",
                        "message": self._provider_catalog_error(result),
                        "status_code": result.get("status_code"),
                    }
                    self._update_provider_check_controls(provider_id)
            details = getattr(self, "llm_discovery_details", None)
            if details is not None:
                details.setPlainText("\n".join(detail_lines))
                details.setToolTip("\n".join(detail_tooltip_lines))
            self._refresh_llm_choices()
            self._refresh_llm_status()
            self._set_llm_preflight_status(
                "ready" if successful_providers else "error",
                providers=successful_providers, models=model_count,
            )

        def _record_provider_catalog(
            self, provider_id: str, catalog: Mapping[str, Any], *, source: str,
        ):
            clean_catalog = dict(catalog)
            models = [str(item) for item in catalog.get("models", ())
                      if isinstance(item, str) and item]
            clean_catalog["provider"] = provider_id
            clean_catalog["models"] = models
            clean_catalog["options"] = [
                dict(option) for option in catalog.get("options", ())
                if isinstance(option, Mapping)
            ]
            self._llm_catalogs[provider_id] = clean_catalog
            self._provider_check_state[provider_id] = {
                "status": "success", "source": source,
                "models": models, "catalog": clean_catalog,
            }
            self._refresh_provider_model_editors(provider_id)
            self._update_provider_check_controls(provider_id)

        def _refresh_provider_model_editors(self, provider_id: str):
            models = list((self._llm_catalogs.get(provider_id) or {}).get("models") or ())
            for editor in self._provider_model_editors.get(provider_id, ()):
                current = editor.currentText()
                previous_block = editor.blockSignals(True)
                editor.clear()
                editor.addItems(models)
                if current and editor.findText(current) < 0:
                    editor.addItem(current)
                index = editor.findText(current) if current else -1
                editor.setCurrentIndex(index)
                if index < 0:
                    editor.setEditText(current)
                editor.blockSignals(previous_block)

        def _apply_provider_check(self, provider_id: str, payload: Any, error: Any):
            request = self._provider_check_requests.pop(provider_id, None)
            if request is None:
                return
            generation, snapshot, updates = request
            if generation != self._provider_check_generations.get(provider_id, 0):
                return
            if (
                snapshot != self._provider_check_inputs_snapshot(provider_id)
                or updates != self._provider_check_updates(provider_id)
            ):
                self._provider_check_state[provider_id] = {"status": "stale"}
                self._update_provider_check_controls(provider_id)
                self._refresh_llm_choices()
                self._refresh_llm_status()
                return
            if error:
                message = text["provider_check_error"]
                self._provider_check_state[provider_id] = {
                    "status": "error", "source": "check", "message": message,
                }
                self._llm_catalogs.pop(provider_id, None)
                self._refresh_provider_model_editors(provider_id)
                self._update_provider_check_controls(provider_id)
                self._show_provider_check_warning(message)
                self._refresh_llm_choices()
                self._refresh_llm_status()
                return
            if not isinstance(payload, Mapping) or payload.get("provider") != provider_id:
                result = {"status": "invalid"}
                ok = False
            else:
                result = payload
                ok = payload.get("ok") is True
            if ok:
                if updates:
                    try:
                        settings_saver(dict(updates))
                    except Exception:
                        # Saver errors can include submitted secret values.
                        message = text["llm_role_save_failed"]
                        self._llm_catalogs.pop(provider_id, None)
                        self._provider_check_state[provider_id] = {
                            "status": "error", "source": "check",
                            "message": message,
                        }
                        self._refresh_provider_model_editors(provider_id)
                        self._update_provider_check_controls(provider_id)
                        self._refresh_llm_choices()
                        self._refresh_llm_status()
                        QtWidgets.QMessageBox.warning(
                            self, text["llm_role_save_failed_title"], message,
                        )
                        return
                    self._mark_provider_check_updates_saved(updates)
                self._record_provider_catalog(provider_id, result, source="check")
                self._refresh_api_key_actions()
                self._refresh_llm_choices()
                self._refresh_llm_status()
                return
            message = self._provider_catalog_error(result)
            self._llm_catalogs.pop(provider_id, None)
            self._provider_check_state[provider_id] = {
                "status": "error", "source": "check", "message": message,
                "status_code": result.get("status_code"),
            }
            self._refresh_provider_model_editors(provider_id)
            self._update_provider_check_controls(provider_id)
            self._refresh_llm_choices()
            self._refresh_llm_status()
            self._show_provider_check_warning(message)

        def _mark_provider_check_updates_saved(self, updates: Mapping[str, str]):
            self._settings_dirty = {
                name: value for name, value in self._settings_dirty.items()
                if name not in updates
            }
            saved_values = {}
            for name, value in updates.items():
                row = next((item for item in self._settings_rows
                            if str(item.get("name") or "") == name), {})
                secret = row.get("control") == "secret" or _is_secret_setting_name(name)
                saved_values[name] = "" if secret else value
                if secret:
                    self._settings_replacing.discard(name)
            self._settings_initial.update(saved_values)

            updated_rows = []
            for row in self._settings_rows:
                name = str(row.get("name") or "")
                if name not in updates:
                    updated_rows.append(row)
                    continue
                value = updates[name]
                secret = row.get("control") == "secret" or _is_secret_setting_name(name)
                safe_value = "" if secret else value
                updated_row = {
                    **row,
                    "value": safe_value,
                    "configured": bool(value),
                    "explicitly_empty": not bool(value),
                }
                if secret:
                    updated_row["display_value"] = "••••••••" if value else ""
                updated_rows.append(updated_row)

                editor = self._settings_editors.get(name)
                if secret and isinstance(editor, QtWidgets.QLineEdit):
                    previous_block = editor.blockSignals(True)
                    editor.setText("••••••••" if value else "")
                    editor.setReadOnly(bool(value))
                    editor.setProperty("secretConfigured", bool(value))
                    editor.setProperty("unconfigured", not bool(value))
                    editor.setPlaceholderText(
                        text["secret_configured"] if value else text["secret_empty"]
                    )
                    editor.blockSignals(previous_block)
                elif isinstance(editor, QtWidgets.QComboBox):
                    previous_block = editor.blockSignals(True)
                    if editor.findText(value) < 0:
                        editor.addItem(value)
                    index = editor.findText(value)
                    if index >= 0:
                        editor.setCurrentIndex(index)
                    else:
                        editor.setEditText(value)
                    editor.blockSignals(previous_block)
                elif isinstance(editor, QtWidgets.QLineEdit):
                    previous_block = editor.blockSignals(True)
                    editor.setText(value)
                    editor.blockSignals(previous_block)
            self._settings_rows = updated_rows

        def _show_provider_check_warning(self, message: str):
            parent = self.llm_choice_dialog if self.llm_choice_dialog.isVisible() else self
            dialog = QtWidgets.QMessageBox(parent)
            dialog.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            dialog.setWindowTitle(text["provider_check_error_title"])
            dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            dialog.setText(str(message))
            dialog.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            dialog.exec()

        def _invalidate_provider_catalog_for_setting(self, name: str):
            provider_ids = {
                str(row.get("check_provider") or "")
                for row in self._settings_rows
                if str(row.get("name") or "") == name
                and (
                    row.get("control") == "secret"
                    or _is_secret_setting_name(name)
                    or name in {"CITATION_VERIFIER_LLM_HOST_COMMAND", "CITATION_VERIFIER_MODEL"}
                    or name.upper().endswith((
                        "_BASE_URL", "_BASE", "_URL", "_HOST", "_ENDPOINT", "_URI",
                    ))
                )
            } - {""}
            if not provider_ids:
                return
            for provider_id in provider_ids:
                self._provider_check_generations[provider_id] = (
                    self._provider_check_generations.get(provider_id, 0) + 1
                )
                self._provider_check_requests.pop(provider_id, None)
                self._invalidate_load(f"provider_check:{provider_id}")
                self._llm_catalogs.pop(provider_id, None)
                self._provider_check_state[provider_id] = {"status": "stale"}
                self._refresh_provider_model_editors(provider_id)
                self._update_provider_check_controls(provider_id)
            if self._llm_preflight_status == "ready":
                self._set_llm_preflight_status("changed")
            self._refresh_llm_choices()
            self._refresh_llm_status()

        def _refresh_llm_choices(self):
            existing_states = {
                selector: (jury1.isChecked(), jury2.isChecked())
                for selector, (jury1, jury2) in self.llm_role_choices.items()
            }
            if not self._llm_role_preferences_loaded:
                self._llm_role_preferences = self._load_llm_role_preferences()
                self._llm_role_preferences_loaded = True
            selected_item = self.llm_choices.currentItem()
            selected_provider = selected_item.data(0, QtCore.Qt.ItemDataRole.UserRole) if selected_item else None
            self.llm_choices.clear()
            self._llm_option_labels = {}
            self.llm_role_choices: dict[str, tuple[Any, Any]] = {}
            self._llm_checkbox_cells = {}
            options = list(verify_candidates_loader() or ())
            known_selectors = {
                str(option.get("selector") or "") for option in options
                if isinstance(option, Mapping)
            }
            for provider_id, catalog in self._llm_catalogs.items():
                for option in catalog.get("options", ()):
                    if not isinstance(option, Mapping):
                        continue
                    selector = str(option.get("selector") or "")
                    if not selector or selector in known_selectors:
                        continue
                    discovered = dict(option)
                    discovered["configured"] = bool(discovered.get("configured"))
                    discovered["_catalog_provider"] = provider_id
                    options.append(discovered)
                    known_selectors.add(selector)
            groups = {}
            def provider_id(option):
                value = str(option.get("_catalog_provider") or option.get("backend") or str(option.get("selector") or "").partition(":")[0])
                return {"ollama_local": "ollama", "freetoken_authenticated": "freetoken", "antigravity_cli": "antigravity"}.get(value, value)

            for provider in sorted({provider_id(option) for option in options if option.get("selector")}, key=lambda value: (
                {"codex_cli": 0, "claude_cli": 1, "openai_compatible": 2}.get(value, 3),
                self._llm_provider_name(value).casefold(),
            )):
                group = QtWidgets.QTreeWidgetItem([self._llm_provider_name(provider)])
                group.setData(0, QtCore.Qt.ItemDataRole.UserRole, provider)
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                self.llm_choices.addTopLevelItem(group)
                group.setExpanded(True)
                groups[provider] = group
            for option in options:
                selector = str(option.get("selector") or "")
                if not selector:
                    continue
                label = str(option.get("label") or selector)
                provider = provider_id(option)
                item = QtWidgets.QTreeWidgetItem(groups[provider], [label])
                item.setToolTip(0, label)
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, provider)
                self._llm_option_labels[selector] = label

                initial_both = False
                if selector in existing_states:
                    initial_states = existing_states[selector]
                elif self._llm_role_preferences is not None:
                    initial_states = (
                        selector in self._llm_role_preferences["jury1"],
                        selector in self._llm_role_preferences["jury2"],
                    )
                else:
                    initial_states = (
                        bool(option.get("jury1_selected", initial_both)),
                        bool(option.get("jury2_selected", initial_both)),
                    )
                jury1_selected, jury2_selected = initial_states
                jury1 = QtWidgets.QCheckBox("", self.llm_choices)
                jury1.setChecked(jury1_selected)
                jury1.setAccessibleName(f"{selector}: {text['jury1_role']}")
                jury1.setAccessibleDescription(text["jury1_lane_tip"])
                jury1.setToolTip(text["jury1_lane_tip"])
                jury1.toggled.connect(
                    lambda checked, selector=selector: self._on_llm_role_toggled(
                        selector, checked, "jury1"
                    )
                )
                jury2 = QtWidgets.QCheckBox("", self.llm_choices)
                jury2.setChecked(jury2_selected)
                jury2.setAccessibleName(f"{selector}: {text['jury2_role']}")
                jury2.setAccessibleDescription(text["jury2_lane_tip"])
                jury2.setToolTip(text["jury2_lane_tip"])
                jury2.toggled.connect(
                    lambda checked, selector=selector: self._on_llm_role_toggled(
                        selector, checked, "jury2"
                    )
                )
                self.llm_choices.setItemWidget(item, 1, self._llm_checkbox_cell(jury1))
                self.llm_choices.setItemWidget(item, 2, self._llm_checkbox_cell(jury2))
                self.llm_role_choices[selector] = (jury1, jury2)
            self._normalize_single_active_model()
            if groups:
                self.llm_choices.setCurrentItem(groups.get(selected_provider) or self.llm_choices.topLevelItem(0))
            self._refresh_jury_model_buttons()
            if hasattr(self, "_atlante_jury_chooser"):
                self._atlante_jury_chooser.refresh()

        def _refresh_jury_model_buttons(self):
            selected = self._selected_llm_roles()
            for role, button in self.jury_model_buttons.items():
                choices = selected[role]
                latest = self._llm_last_selected.get(role)
                selector = latest if latest in choices else (choices[-1] if choices else None)
                label = self._llm_option_labels.get(selector, selector) if selector else text["jury_not_selected"]
                suffix = f" +{len(choices)-1}" if len(choices) > 1 else ""
                visible_label = button.fontMetrics().elidedText(label, QtCore.Qt.TextElideMode.ElideRight, max(240, button.width() - 100))
                button.setText(f"{text[role + '_role']}: {visible_label}{suffix}")
                button.setToolTip("\n".join(self._llm_option_labels.get(item, item) for item in choices) or text["jury_not_selected"])
                button.setEnabled(self._process is None)
            self._refresh_jury_action_role_buttons()

        def _refresh_jury_action_role_buttons(self):
            dialog = getattr(self, "_jury_action_dialog", None)
            buttons = getattr(self, "_jury_action_role_buttons", None)
            if dialog is None or buttons is None:
                return
            selected = self._selected_llm_roles()
            for role, button in buttons.items():
                choices = selected[role]
                latest = self._llm_last_selected.get(role)
                selector = latest if latest in choices else (choices[-1] if choices else None)
                label = (
                    self._llm_option_labels.get(selector, selector)
                    if selector else text["jury_not_selected"]
                )
                suffix = f" +{len(choices)-1}" if len(choices) > 1 else ""
                full_label = f"{text[role + '_role']}: {label}{suffix}"
                button.setText(button.fontMetrics().elidedText(
                    full_label, QtCore.Qt.TextElideMode.ElideRight,
                    max(1, button.width() - 24),
                ))
                button.setAccessibleName(full_label)
                button.setToolTip(
                    "\n".join(self._llm_option_labels.get(item, item) for item in choices)
                    or text["jury_not_selected"]
                )

        def _set_jury_picker_parent(self, parent):
            flags = (
                self.llm_choice_dialog.windowFlags()
                if sys.platform == "darwin"
                else QtCore.Qt.WindowType.Dialog
            )
            self.llm_choice_dialog.setParent(parent, flags)

        def _show_jury_models(self, role: str, *, modal_parent=None):
            if self._process is not None:
                return
            self._llm_picker_role = role
            if not self._settings_loaded:
                if self._background_loading:
                    self._queue_load("settings", settings_loader)
                else:
                    self._apply_settings_payload(settings_loader() or {})
            self._atlante_jury_chooser.refresh_settings()
            self._atlante_jury_chooser.refresh(role)
            if not getattr(self, "_jury_first_session_finished_connected", False):
                self.llm_choice_dialog.finished.connect(
                    self._finish_initial_jury1_session
                )
                self._jury_first_session_finished_connected = True
            settings = QtCore.QSettings("Callimachus", "Callimachus")
            if (
                role == "jury1"
                and not self._jury_first_jury1_session
                and not self._jury_action_selection_active
                and not settings.value(
                    _JURY1_CHOOSER_FIRST_SESSION_COMPLETE_KEY, False, type=bool,
                )
            ):
                self._jury_first_jury1_session = True
                has_saved_roles = settings.contains(_LLM_ROLE_PREFERENCE_KEY)
                has_configured_roles = any(
                    bool(selectors)
                    for selectors in self._selected_llm_roles().values()
                )
                has_loaded_roles = bool(
                    self._llm_role_preferences
                    and any(self._llm_role_preferences.values())
                )
                self._jury_initial_mirror_active = not (
                    has_saved_roles or has_configured_roles or has_loaded_roles
                )
            self.llm_choice_dialog.setWindowTitle(text.get("atlas_choose_jury", "Choose Jury {number}").format(number=1 if role == "jury1" else 2))
            self.llm_choices.setColumnHidden(1, role != "jury1")
            self.llm_choices.setColumnHidden(2, role != "jury2")
            self.llm_choice_dialog.setStyleSheet(self.styleSheet())
            self.llm_choice_dialog.setFont(self.font())
            if modal_parent is not None:
                if not getattr(self, "_jury_picker_parent_restore_connected", False):
                    self.llm_choice_dialog.finished.connect(
                        self._restore_jury_picker_parent
                    )
                    self._jury_picker_parent_restore_connected = True
                self._jury_picker_modal_parent = modal_parent
                self._set_jury_picker_parent(modal_parent)
            elif self.llm_choice_dialog.parentWidget() is not self:
                self._set_jury_picker_parent(self)
                self._jury_picker_modal_parent = None
            self._atlante_jury_chooser._fit_dialog_to_parent()
            self._refresh_jury_picker_check()
            backdrop = getattr(self,"_atlas_jury_backdrop",None)
            if modal_parent is None and backdrop is None:
                backdrop = QtWidgets.QWidget(self.centralWidget())
                backdrop.setObjectName("juryBackdrop")
                backdrop.setStyleSheet("background-color: rgba(30, 46, 70, 75);")
                self._atlas_jury_backdrop = backdrop
                self.llm_choice_dialog.finished.connect(backdrop.hide)
            if modal_parent is None:
                backdrop.setGeometry(self.centralWidget().rect())
                backdrop.show()
                backdrop.raise_()
            elif backdrop is not None:
                backdrop.hide()
            self.llm_choice_dialog.setWindowModality(
                QtCore.Qt.WindowModality.ApplicationModal
                if sys.platform == "darwin"
                else QtCore.Qt.WindowModality.WindowModal
            )
            self.llm_choice_dialog.show()
            self.llm_choice_dialog.raise_()
            self.llm_choice_dialog.activateWindow()

        def _finish_initial_jury1_session(self, *_args):
            if (
                not self._jury_first_jury1_session
                or self._jury_action_selection_active
            ):
                return
            self._jury_first_jury1_session = False
            self._jury_initial_mirror_active = False
            settings = QtCore.QSettings("Callimachus", "Callimachus")
            settings.setValue(
                _JURY1_CHOOSER_FIRST_SESSION_COMPLETE_KEY, True,
            )
            settings.sync()

        def _mirror_initial_jury1_toggle(self, selector: str, checked: bool):
            if (
                not self._jury_initial_mirror_active
                or self._jury_action_selection_active
                or self._llm_picker_role != "jury1"
            ):
                return
            choices = self.llm_role_choices.get(selector)
            if choices is None:
                return
            jury2 = choices[1]
            previous_block = jury2.blockSignals(True)
            self._normalizing_llm_roles = True
            try:
                jury2.setChecked(checked)
            finally:
                self._normalizing_llm_roles = False
                jury2.blockSignals(previous_block)
            if checked:
                self._llm_last_selected["jury2"] = selector
            elif self._llm_last_selected.get("jury2") == selector:
                self._llm_last_selected["jury2"] = next(
                    (item for item, (_jury1, box) in self.llm_role_choices.items()
                     if box.isChecked()),
                    None,
                )

        def _restore_jury_picker_parent(self, *_args):
            modal_parent = getattr(self, "_jury_picker_modal_parent", None)
            if modal_parent is None:
                return
            self._jury_picker_modal_parent = None
            self._set_jury_picker_parent(self)
            self.llm_choice_dialog.setWindowModality(
                QtCore.Qt.WindowModality.ApplicationModal
                if sys.platform == "darwin"
                else QtCore.Qt.WindowModality.WindowModal
            )
            try:
                modal_parent.raise_()
                modal_parent.activateWindow()
            except RuntimeError:
                pass

        def _jury_picker_provider(self):
            if hasattr(self, "_atlante_jury_chooser"):
                return self._atlante_jury_chooser.current_provider()
            item = self.llm_choices.currentItem()
            return str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "") if item else ""

        def _refresh_jury_picker_check(self, *_args):
            if not hasattr(self, "llm_picker_check"):
                return
            provider = self._jury_picker_provider()
            state = self._provider_check_state.get(provider) or {}
            self.llm_picker_check.setEnabled(
                not self._load_closed and state.get("status") != "checking"
                and (provider_check_callback is not None or llm_preflight_loader is not None)
            )
            self.llm_picker_status.setText(
                f"{self._llm_provider_name(provider)}: {self._provider_status_text(provider)}"
                if provider else text["jury_picker_help"]
            )
            self.llm_picker_check.setProperty("providerCheckState", state.get("status", "idle"))
            self.llm_picker_check.style().unpolish(self.llm_picker_check)
            self.llm_picker_check.style().polish(self.llm_picker_check)
            if hasattr(self, "_atlante_jury_chooser"):
                self._atlante_jury_chooser._refresh_empty_catalog_hint()

        def _check_jury_models(self):
            provider = self._jury_picker_provider()
            if provider and provider_check_callback is not None:
                self._start_provider_check(provider)
            elif llm_preflight_loader is not None:
                self._start_llm_preflight()

        def _load_llm_role_preferences(self) -> dict[str, set[str]] | None:
            settings = QtCore.QSettings("Callimachus", "Callimachus")
            if not settings.contains(_LLM_ROLE_PREFERENCE_KEY):
                return None
            try:
                raw = settings.value(_LLM_ROLE_PREFERENCE_KEY)
                if not isinstance(raw, str):
                    raise ValueError("stored role preference is not JSON text")
                data = json.loads(raw)
            except (TypeError, ValueError):
                self._llm_role_preferences_invalid = True
                return {"jury1": set(), "jury2": set()}
            if type(data) is not dict or set(data) != {"jury1", "jury2"}:
                self._llm_role_preferences_invalid = True
                return {"jury1": set(), "jury2": set()}
            preferences: dict[str, set[str]] = {}
            for role in ("jury1", "jury2"):
                selectors = data[role]
                if type(selectors) is not list or any(
                    type(selector) is not str or not selector
                    for selector in selectors
                ):
                    self._llm_role_preferences_invalid = True
                    return {"jury1": set(), "jury2": set()}
                preferences[role] = set(selectors)
            return preferences

        def _save_llm_role_preferences(self) -> bool:
            selected = self._selected_llm_roles()
            settings = QtCore.QSettings("Callimachus", "Callimachus")
            settings.setValue(
                _LLM_ROLE_PREFERENCE_KEY,
                json.dumps(selected, separators=(",", ":")),
            )
            settings.sync()
            if settings.status() != QtCore.QSettings.Status.NoError:
                return False
            self._llm_role_preferences = {
                role: set(selectors) for role, selectors in selected.items()
            }
            self._llm_role_preferences_loaded = True
            self._llm_role_preferences_invalid = False
            return True

        def _set_llm_role_selection(
            self, selected: Mapping[str, Sequence[str]], *, last_selected=None,
        ):
            selected_by_role = {
                role: set(str(item) for item in selected.get(role, ()))
                for role in ("jury1", "jury2")
            }
            self._normalizing_llm_roles = True
            try:
                for selector, (jury1, jury2) in self.llm_role_choices.items():
                    for role, checkbox in (("jury1", jury1), ("jury2", jury2)):
                        previous_block = checkbox.blockSignals(True)
                        checkbox.setChecked(selector in selected_by_role[role])
                        checkbox.blockSignals(previous_block)
            finally:
                self._normalizing_llm_roles = False
            if last_selected is not None:
                self._llm_last_selected = dict(last_selected)
            else:
                for role in ("jury1", "jury2"):
                    current = self._llm_last_selected.get(role)
                    if current not in selected_by_role[role]:
                        self._llm_last_selected[role] = next(
                            (selector for selector in selected.get(role, ())), None
                        )
            self._refresh_llm_status()
            if hasattr(self, "_atlante_jury_chooser"):
                self._atlante_jury_chooser.refresh()

        def _begin_jury_action_selection(self):
            if self._jury_action_selection_transaction is not None:
                return False
            preferences_loaded = self._llm_role_preferences_loaded
            preferences_before = self._llm_role_preferences
            if not self._llm_role_preferences_loaded:
                self._llm_role_preferences = self._load_llm_role_preferences()
                self._llm_role_preferences_loaded = True
            preferences = self._llm_role_preferences
            has_saved_roles = bool(
                preferences
                and any(preferences.get(role) for role in ("jury1", "jury2"))
            )
            original_roles = self._selected_llm_roles()
            transaction = {
                "roles": original_roles,
                "has_saved_roles": has_saved_roles or any(original_roles.values()),
                "last_selected": dict(self._llm_last_selected),
                "preferences": (
                    None if preferences_before is None else {
                        role: set(selectors) for role, selectors in preferences_before.items()
                    }
                ),
                "preferences_loaded": preferences_loaded,
                "preferences_invalid": self._llm_role_preferences_invalid,
            }
            self._jury_action_selection_transaction = transaction
            self._jury_action_selection_active = True
            return True

        def _finish_jury_action_selection(
            self, *, confirmed: bool, preview: Mapping[str, Any] | None = None,
        ) -> bool:
            transaction = self._jury_action_selection_transaction
            if transaction is None:
                return False
            if confirmed and not transaction["has_saved_roles"]:
                self._jury_action_selection_active = False
                overlay_saved = self._persist_selected_llm_overlay(
                    preview if isinstance(preview, Mapping) else {}
                )
                preferences_saved = overlay_saved and self._save_llm_role_preferences()
                if not preferences_saved:
                    self._jury_action_selection_transaction = None
                    self._restore_jury_action_selection(transaction)
                    QtWidgets.QMessageBox.warning(
                        self, text["llm_role_save_failed_title"],
                        text["llm_role_save_failed"],
                    )
                    return False
                self._jury_action_selection_transaction = None
                self._refresh_llm_status(preview=preview)
                return True
            self._jury_action_selection_transaction = None
            self._restore_jury_action_selection(transaction)
            return bool(confirmed)

        def _restore_jury_action_selection(self, transaction):
            self._jury_action_selection_active = False
            self._llm_role_preferences = transaction["preferences"]
            self._llm_role_preferences_loaded = transaction["preferences_loaded"]
            self._llm_role_preferences_invalid = transaction["preferences_invalid"]
            self._llm_last_selected = dict(transaction["last_selected"])
            self._set_llm_role_selection(
                transaction["roles"], last_selected=transaction["last_selected"]
            )

        def _llm_checkbox_cell(self, checkbox):
            cell = QtWidgets.QWidget(self.llm_choices)
            layout = QtWidgets.QHBoxLayout(cell)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(checkbox, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
            cell.setToolTip(checkbox.toolTip())
            cell.installEventFilter(self)
            self._llm_checkbox_cells[cell] = checkbox
            return cell

        def _normalize_single_active_model(self):
            # Role choices are independent, including a lone selected model.
            return

        def _on_llm_role_toggled(self, selector: str, checked: bool, role: str):
            if self._normalizing_llm_roles:
                return
            if checked:
                self._llm_last_selected[role] = selector
            elif (
                role == "jury1"
                and self._jury_initial_mirror_active
                and self._llm_last_selected.get(role) == selector
            ):
                self._llm_last_selected[role] = next(
                    (item for item, (jury1, jury2) in self.llm_role_choices.items()
                     if (jury1 if role == "jury1" else jury2).isChecked()),
                    None,
                )
            if role == "jury1":
                self._mirror_initial_jury1_toggle(selector, checked)
            preview = self._selected_llm_preview()
            if self._jury_action_selection_active:
                self._refresh_llm_status(preview=preview)
                if hasattr(self, "_atlante_jury_chooser"):
                    self._atlante_jury_chooser.refresh()
                return
            saved = self._save_llm_role_preferences()
            overlay_saved = self._persist_selected_llm_overlay(preview)
            self._refresh_llm_status(preview=preview)
            if hasattr(self, "_atlante_jury_chooser"):
                self._atlante_jury_chooser.refresh()
            if not saved or not overlay_saved:
                QtWidgets.QMessageBox.warning(
                    self,
                    text["llm_role_save_failed_title"],
                    text["llm_role_save_failed"],
                )

        def _selected_llm_roles(self) -> dict[str, list[str]]:
            selected = {"jury1": [], "jury2": []}
            for selector, (jury1, jury2) in self.llm_role_choices.items():
                if jury1.isChecked():
                    selected["jury1"].append(selector)
                if jury2.isChecked():
                    selected["jury2"].append(selector)
            return selected

        def _selected_llm_preview(self):
            try:
                if verify_catalog_preview_loader is not None:
                    return dict(verify_catalog_preview_loader(
                        dict(self._llm_catalogs), self._selected_llm_roles(),
                        self._selected_jury_level(),
                    ) or {})
                return dict(verify_selection_preview_loader(
                    self._selected_llm_roles(), self._selected_jury_level()
                ) or {})
            except Exception:
                return {"available": False, "reason": "no_verify_backend", "overlay": {}}

        def _persist_selected_llm_overlay(self, preview: Mapping[str, Any]) -> bool:
            if (
                self._jury_action_selection_active
                or
                verify_catalog_preview_loader is None
                or self._process is not None
                or not preview.get("available")
            ):
                return True
            overlay = preview.get("overlay")
            if not isinstance(overlay, Mapping) or not overlay:
                return True
            updates = {str(name): str(value) for name, value in overlay.items()}
            if any(_is_secret_setting_name(name) for name in updates):
                return False
            try:
                settings_saver(updates)
            except Exception:
                return False
            self._settings_dirty = {
                name: value for name, value in self._settings_dirty.items()
                if name not in updates
            }
            self._settings_initial.update(updates)
            updated_rows = []
            for row in self._settings_rows:
                name = str(row.get("name") or "")
                if name in updates:
                    updated_row = {**row, "value": updates[name]}
                    if row.get("model_provider"):
                        updated_row["configured"] = bool(updates[name])
                    updated_rows.append(updated_row)
                    editor = self._settings_editors.get(name)
                    if isinstance(editor, QtWidgets.QComboBox):
                        previous_block = editor.blockSignals(True)
                        if editor.findText(updates[name]) < 0:
                            editor.addItem(updates[name])
                        index = editor.findText(updates[name])
                        if index >= 0:
                            editor.setCurrentIndex(index)
                        else:
                            editor.setEditText(updates[name])
                        editor.blockSignals(previous_block)
                else:
                    updated_rows.append(row)
            self._settings_rows = updated_rows
            return True

        def _refresh_llm_status(self, *_args, preview=None):
            self._refresh_jury_model_buttons()
            preview = preview if isinstance(preview, Mapping) else self._selected_llm_preview()
            available = bool(preview.get("available"))
            reason = str(preview.get("reason") or "")
            if self._llm_role_preferences_invalid:
                status = text["llm_role_preferences_invalid"]
            elif available:
                status = text["llm_ready"]
            elif "jury1" in reason.casefold() and "no eligible lane" in reason.casefold():
                status = text["llm_jury1_required"]
            elif reason and reason != "no_verify_backend":
                status = text["llm_invalid_reason"].format(reason=reason)
            else:
                status = text["llm_unavailable"]
            self.llm_status.setText(status)
            can_change = self._process is None
            for jury1, jury2 in self.llm_role_choices.values():
                jury1.setEnabled(can_change)
                jury2.setEnabled(can_change)
            for button in self.jury_buttons:
                button.setEnabled(can_change)
            for proxy in getattr(self, "_atlas_options_controls", {}).get("severity", ()):
                proxy.setEnabled(can_change)

        def set_run_dir(self, run_dir: str):
            """Attach an existing run for display or history-driven resume."""
            if (
                self._pending_child_source_dir
                and self._pending_child_source_dir != run_dir
            ):
                self._pending_child_source_dir = None
            self._cancel_source_detail_next()
            self._display_run_done = False
            self._display_references_only = None
            if self._detail_dialog is not None:
                self._detail_dialog[0].close()
            if self._background_loading:
                self._atlas_source_cancel.set()
                self._invalidate_load("snapshot")
                self._snapshot_loading_run = None
                self._snapshot_loaded_run = None
                self._invalidate_load("source_page")
                self._invalidate_load("source_refresh")
                self._invalidate_load("source_query")
                self._invalidate_load("source_query_refresh")
                self._source_query_timer.stop()
                self._source_paged_rows = []
                self._source_paged_total = 0
                self._source_next_offset = 0
                self._source_page_loaded_run = None
                self._source_overview_signature = None
                self._source_query_signature = None
                self._source_query_refresh_failed = False
                self._source_query_refresh_pending = False
                self._source_page_stale = False
                self._next_snapshot_at = 0.0
                self._source_rows = []
                self.source_table.setRowCount(0)
                self.source_stack.setCurrentIndex(1)
                self._display_run_done = False
            self._run_dir = run_dir
            self._guided_fetch_available = False
            self._parse_review_available = False
            self._parse_review_pending = 0
            self._set_error_state(False)
            if self._background_loading:
                self._request_snapshot(force=True)
                if self._source_page_mode:
                    if self._source_query_active():
                        self._request_source_query()
                    else:
                        self._request_source_page()
            else:
                self._refresh_snapshot()

        def _sort_sources(self, column: int):
            if not 0 <= column <= 7:
                return
            self._cancel_source_detail_next()
            if self._source_sort_column == column:
                self._source_sort_ascending = not self._source_sort_ascending
            else:
                self._source_sort_column = column
                self._source_sort_ascending = True
            order = (
                QtCore.Qt.SortOrder.AscendingOrder
                if self._source_sort_ascending
                else QtCore.Qt.SortOrder.DescendingOrder
            )
            self.source_table.horizontalHeader().setSortIndicator(column, order)
            self._source_visible_count = self._page_batch_size(self.source_table)
            if self._source_page_mode:
                self._request_source_query()
            else:
                self._render_sources()

        def _render_sources(self):
            query = self.source_search.text().casefold().strip()
            selected = self.source_filter.currentText()
            selected_key = {text["all"]: "", text["fulltext"]: "fulltext", text["abstract"]: "abstract", text["no_text"]: "none"}.get(selected, "")
            selected_phase = str(
                self.source_phase_filter.currentData() or ""
            ).casefold()
            rows = []
            for appearance_order, row in enumerate(self._source_rows, start=1):
                parsed = row.get("parsed")
                if not isinstance(parsed, Mapping):
                    details = row.get("details")
                    parsed = details.get("parsed") if isinstance(details, Mapping) else {}
                if not isinstance(parsed, Mapping):
                    parsed = {}
                source_number = appearance_order
                for candidate in (row.get("ref_number"), parsed.get("ref_number")):
                    if isinstance(candidate, bool):
                        continue
                    if isinstance(candidate, int) and candidate > 0:
                        source_number = candidate
                        break
                    if (
                        isinstance(candidate, float)
                        and math.isfinite(candidate)
                        and candidate > 0
                        and candidate.is_integer()
                    ):
                        source_number = int(candidate)
                        break
                availability = str(row.get("text") or row.get("availability") or row.get("tier") or "none").lower()
                haystack = " ".join(str(row.get(key, "")) for key in ("title", "source", "ref_id", "status", "result")).casefold()
                if query and query not in haystack:
                    continue
                from .atlante import matches_source
                if not matches_source(row, filters=getattr(self, "_atlas_filter_keys", ())):
                    continue
                if selected_key and availability != selected_key:
                    continue
                if selected_phase and str(row.get("phase") or "").casefold() != selected_phase:
                    continue
                rows.append((row, source_number))
            if self._source_sort_column is not None:
                def sort_key(entry):
                    row, source_number = entry
                    if self._source_sort_column == 0:
                        return source_number
                    values = {
                        1: row.get("title") or row.get("source") or row.get("raw_entry") or text["unavailable"],
                        2: row.get("phase") or text["unavailable"],
                        3: row.get("status") or text["unavailable"],
                        4: row.get("text") or row.get("availability") or row.get("tier") or text["unavailable"],
                        5: row.get("risk_signal") or "",
                        6: row.get("review_labels") or "",
                        7: row.get("result") or row.get("verdict") or text["unavailable"],
                    }
                    return str(values.get(self._source_sort_column, "")).casefold()

                rows.sort(
                    key=sort_key,
                    reverse=not self._source_sort_ascending,
                )
            if self._source_visible_count <= 0:
                self._source_visible_count = self._page_batch_size(self.source_table)
            more_available = (
                self._source_next_offset < self._source_paged_total
                if self._source_page_mode and not self._source_query_active()
                else self._atlas_query_has_more if self._source_page_mode
                else len(rows) > self._source_visible_count
            )
            self.source_more_button.setVisible(more_available)
            visible_rows = rows[:self._source_visible_count]
            new_ids = [str(row.get("ref_id") or "") for row, _number in visible_rows]
            new_id_set = set(new_ids)
            old_ids = []
            for index in range(self.source_table.rowCount()):
                item = self.source_table.item(index, 0)
                payload = (
                    item.data(QtCore.Qt.ItemDataRole.UserRole)
                    if item is not None else None
                )
                old_ids.append(
                    str(payload.get("ref_id") or "")
                    if isinstance(payload, Mapping) else ""
                )
            stable_ids = (
                all(new_ids) and len(set(new_ids)) == len(new_ids)
            )
            same_visible_rows = (
                len(old_ids) == len(new_ids)
                and old_ids == new_ids
                and (not new_ids or stable_ids)
            )
            current_row = self.source_table.currentRow()
            current_payload = (
                self.source_table.item(current_row, 0).data(
                    QtCore.Qt.ItemDataRole.UserRole
                )
                if current_row >= 0 and self.source_table.item(current_row, 0) is not None
                else None
            )
            current_ref_id = (
                str(current_payload.get("ref_id") or "")
                if isinstance(current_payload, Mapping) else ""
            )
            if not same_visible_rows:
                if self._detail_dialog is not None:
                    detail_ref_id = str(self._detail_dialog[2].get("ref_id") or "")
                    if detail_ref_id not in new_id_set:
                        self._detail_dialog[0].close()
                self.source_table.setRowCount(len(visible_rows))
            for index, (row, source_number) in enumerate(visible_rows):
                raw_availability = str(
                    row.get("text")
                    or row.get("availability")
                    or row.get("tier")
                    or "none"
                ).lower()
                availability = {
                    "fulltext": text["fulltext"],
                    "abstract": text["abstract"],
                    "none": text["no_text"],
                }.get(raw_availability, raw_availability)
                raw_status = str(row.get("status") or "")
                result_value = row.get("result") or row.get("verdict")
                has_open_verification_pairs = row.get("verification_pending") is True
                has_active_verification_pair = row.get("verification_in_progress") is True
                if (
                    str(row.get("phase") or "").casefold() == "verify"
                    and raw_status.casefold() == "in_corso"
                    and has_active_verification_pair
                ):
                    result_value = (
                        f"{result_value} · {text['status_running']}"
                        if result_value else text["status_running"]
                    )
                elif (
                    getattr(self, "_atlas_current_phase", None) == "verify"
                    and not has_active_verification_pair
                    and (
                        has_open_verification_pairs
                        or not result_value
                    )
                ):
                    result_value = (
                        f"{result_value} · {text['status_waiting']}"
                        if result_value and has_open_verification_pairs
                        else result_value or text["status_waiting"]
                    )
                status = {
                    "completato": f"✓ {text['status_complete']}",
                    "in_corso": (
                        f"{'◐◓◑◒'[self._spinner_frame % 4]} {text['status_running']}"
                    ),
                    "richiede_intervento": f"! {text['status_action_required']}",
                    "in_attesa": f"○ {text['status_waiting']}",
                }.get(raw_status.casefold(), raw_status or text["unavailable"])
                values = (source_number, row.get("title") or row.get("source") or row.get("raw_entry") or text["unavailable"], row.get("phase") or text["unavailable"], status, availability, row.get("risk_signal") or "", " · ".join(str(value) for value in (row.get("risk_signal"), row.get("review_labels")) if value), result_value or text["unavailable"], text["details"])
                for column, value in enumerate(values):
                    item = (
                        self.source_table.item(index, column)
                        if same_visible_rows else None
                    )
                    if item is None:
                        item = QtWidgets.QTableWidgetItem()
                        self.source_table.setItem(index, column, item)
                    cell_text = str(value)
                    if item.text() != cell_text:
                        item.setText(cell_text)
                    if column == 0:
                        payload = dict(row)
                        if item.data(QtCore.Qt.ItemDataRole.UserRole) != payload:
                            item.setData(QtCore.Qt.ItemDataRole.UserRole, payload)
                    background = QtGui.QBrush()
                    foreground = QtGui.QBrush()
                    tooltip = ""
                    show_risk_icon = False
                    if column in {3, 4}:
                        normalized = raw_status.casefold() if column == 3 else raw_availability
                        color = (
                            "#d9f5e4"
                            if normalized in {"completato", "fulltext"}
                            else "#fff0c7"
                            if normalized in {
                                "in_corso",
                                "richiede_intervento",
                                "abstract",
                            }
                            else "#e8edf4"
                        )
                        background = QtGui.QBrush(QtGui.QColor(color))
                        foreground = QtGui.QBrush(QtGui.QColor("#18202a"))
                    if column == 5 and value:
                        foreground = QtGui.QBrush(QtGui.QColor("#b42318"))
                        tooltip = str(row.get("risk_tooltip") or "")
                    if column == 1 and row.get("risk_signal"):
                        show_risk_icon = True
                        tooltip = str(value) + "\n" + str(row.get("risk_tooltip") or row["risk_signal"])
                    if column == 6 and value:
                        foreground = QtGui.QBrush(QtGui.QColor(
                            "#b42318" if row.get("risk_signal") else "#b15c00"
                        ))
                        tooltip = "\n".join(str(value) for value in (
                            row.get("risk_tooltip"), row.get("review_tooltip")
                        ) if value)
                    if item.background() != background:
                        item.setBackground(background)
                    if item.foreground() != foreground:
                        item.setForeground(foreground)
                    if item.toolTip() != tooltip:
                        item.setToolTip(tooltip)
                    current_icon = item.icon()
                    if show_risk_icon and current_icon.isNull():
                        from .atlas_icons import icon as atlas_icon
                        item.setIcon(atlas_icon(QtCore, QtGui, "shield", "#b42318", 18))
                    elif not show_risk_icon and not current_icon.isNull():
                        item.setIcon(QtGui.QIcon())
            if not same_visible_rows:
                self.source_table.clearSelection()
                self.source_table.setCurrentCell(-1, -1)
                if current_ref_id and current_ref_id in new_ids:
                    # Identity changed, so do not leave the old row index
                    # pointing at a different source after sorting/filtering.
                    self.source_table.setCurrentCell(new_ids.index(current_ref_id), 0)
            if self._detail_dialog is not None:
                detail_ref_id = self._detail_dialog[2].get("ref_id")
                if str(detail_ref_id or "") in new_id_set:
                    latest = next(
                        row for row, _number in visible_rows
                        if row.get("ref_id") == detail_ref_id
                    )
                    details = self._detail_dialog[2].get("details")
                    self._detail_dialog[2].update(dict(latest))
                    if details is not None:
                        self._detail_dialog[2]["details"] = details
            self._update_source_detail_navigation()

        def _render_source_summary(self, view, row):
            detail = getattr(self, "_detail_dialog", None)
            is_open_detail = detail is not None and detail[1] is view
            compact = bool(
                is_open_detail
                and getattr(self, "_atlas_source_detail_compact", False)
            )
            if is_open_detail:
                heading = detail[0].findChild(
                    QtWidgets.QLabel, "sourceDetailHeadingTitle",
                )
                if heading is not None:
                    heading.setText(_source_summary_title(row, text))
                    header = heading.parentWidget()
                    if header is not None:
                        self._sync_source_detail_heading_alignment(header)
                row["_source_summary_heading_in_view"] = compact
            view.setHtml(_source_summary_html(
                row, text, dark=self._dark, font_size=self._font_size,
                include_title=not is_open_detail or compact,
                compact_metadata=view.viewport().width() < max(420, self._font_size * 28),
            ))
            from .source_summary import configure_summary_frames
            configure_summary_frames(view, dark=self._dark)
            if hasattr(view, "configure_summary_reflow"):
                view.configure_summary_reflow(
                    lambda: self._render_source_summary(view, row),
                    max(420, self._font_size * 28),
                )
            block = view.document().begin()
            while block.isValid():
                block_format = block.blockFormat()
                if block.blockFormat().headingLevel() and not block.begin().atEnd():
                    character = block.begin().fragment().charFormat()
                    # Qt adds a relative heading scale even with an explicit
                    # pixel size. Keep the intended, accessible Guide type scale.
                    character.clearProperty(QtGui.QTextFormat.Property.FontSizeAdjustment)
                    cursor = QtGui.QTextCursor(block)
                    cursor.select(QtGui.QTextCursor.SelectionType.BlockUnderCursor)
                    cursor.setCharFormat(character)
                if block.text().strip() and block_format.headingLevel() == 0:
                    block_format.setLineHeight(
                        160.0,
                        QtGui.QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
                    )
                    QtGui.QTextCursor(block).setBlockFormat(block_format)
                block = block.next()

        def _sync_source_detail_heading_alignment(self, header):
            title = header.findChild(QtWidgets.QLabel, "sourceDetailHeadingTitle")
            close = header.findChild(QtWidgets.QToolButton, "sourceDetailClose")
            if title is None or close is None:
                return
            button_height = close.height() or close.sizeHint().height()
            text_height = title.fontMetrics().height()
            top_inset = max(0, int(round((button_height - text_height) / 2)))
            margins = title.contentsMargins()
            if margins.top() != top_inset:
                title.setContentsMargins(
                    margins.left(), top_inset, margins.right(), margins.bottom(),
                )

        def _connect_source_summary_links(self, view, row):
            view.setOpenLinks(False)
            view.anchorClicked.connect(
                lambda url, target=view, source_row=row:
                self._toggle_source_summary_verdict(target, source_row, url)
            )

        def _toggle_source_summary_verdict(self, view, row, url):
            if (
                url.scheme().casefold() != "callimachus"
                or url.userInfo()
                or url.port(-1) != -1
                or url.query()
                or url.fragment()
            ):
                return
            host = url.host().casefold()
            if host == "source-details":
                if url.path() != "/technical":
                    return
                row["_summary_technical_details_expanded"] = not bool(
                    row.get("_summary_technical_details_expanded")
                )
                scroll_position = view.verticalScrollBar().value()
                self._render_source_summary(view, row)
                view.verticalScrollBar().setValue(scroll_position)
                view.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
                return
            if host != "source-verdict":
                return
            match = re.fullmatch(r"/(0|[1-9][0-9]*)", url.path())
            if match is None:
                return
            details = row.get("details")
            details = details if isinstance(details, Mapping) else {}
            pairs = details.get("verification_pairs")
            if not isinstance(pairs, list):
                return
            index = int(match.group(1))
            if index >= len(pairs) or not isinstance(pairs[index], Mapping):
                return
            pair = pairs[index]
            parsed = details.get("parsed")
            parsed = parsed if isinstance(parsed, Mapping) else {}
            bound_ref_id = str(row.get("ref_id") or parsed.get("ref_id") or "")
            pair_ref_id = str(pair.get("ref_id") or bound_ref_id)
            if not bound_ref_id or pair_ref_id != bound_ref_id:
                return
            pair_key = (
                str(pair.get("claim_id") or ""),
                pair_ref_id,
                str(pair.get("scope") or ""),
            )
            expanded = row.get("_summary_expanded_verification_pairs")
            expanded = set(expanded) if isinstance(expanded, set) else set()
            if pair_key in expanded:
                expanded.remove(pair_key)
            else:
                expanded.add(pair_key)
            row["_summary_expanded_verification_pairs"] = expanded
            scroll_position = view.verticalScrollBar().value()
            self._render_source_summary(view, row)
            view.verticalScrollBar().setValue(scroll_position)
            view.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

        def _source_detail_navigation_signature(self):
            return (
                self._run_dir,
                self.source_search.text(),
                self.source_filter.currentText(),
                self.source_phase_filter.currentData(),
                tuple(sorted(getattr(self, "_atlas_filter_keys", ()))),
                self._source_sort_column,
                self._source_sort_ascending,
            )

        def _source_detail_has_more(self):
            if self._source_page_mode:
                if self._source_query_active():
                    return bool(self._atlas_query_has_more)
                return self._source_next_offset < self._source_paged_total
            return len(self._source_rows) > self._source_visible_count

        def _source_detail_row_index(self, ref_id):
            for index in range(self.source_table.rowCount()):
                item = self.source_table.item(index, 0)
                payload = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
                if isinstance(payload, Mapping) and str(payload.get("ref_id") or "") == str(ref_id):
                    return index
            return -1

        def _update_source_detail_navigation(self):
            button = getattr(self, "_atlas_source_detail_next_button", None)
            if button is None:
                return
            detail = self._detail_dialog
            compact_open = bool(
                getattr(self, "_atlas_source_detail_compact", False)
                and getattr(self, "_atlas_source_detail_overlay_open", False)
            )
            if detail is None:
                button.hide()
                button.setEnabled(False)
                return
            row_index = self._source_detail_row_index(
                detail[2].get("ref_id") or ""
            )
            loaded_next = row_index >= 0 and row_index + 1 < self.source_table.rowCount()
            more = self._source_detail_has_more()
            loading = bool(
                "source_page" in self._loading_kinds
                or "source_query" in self._loading_kinds
            )
            can_advance = loaded_next or more
            button.setEnabled(can_advance and (loaded_next or not loading))
            button.setVisible(compact_open and (can_advance or loading))

        def _cancel_source_detail_next(self):
            self._source_detail_navigation_generation += 1
            self._source_detail_next_pending = None
            self._update_source_detail_navigation()

        def _source_detail_back(self):
            self._cancel_source_detail_next()
            hide_overlay = getattr(self, "_atlas_hide_source_detail_overlay", None)
            if hide_overlay is not None:
                hide_overlay()

        def _show_next_source_detail(self):
            detail = self._detail_dialog
            if detail is None or not getattr(self, "_atlas_source_detail_compact", False):
                return
            ref_id = str(detail[2].get("ref_id") or "")
            row_index = self._source_detail_row_index(ref_id)
            if row_index < 0:
                self._update_source_detail_navigation()
                return
            if row_index + 1 < self.source_table.rowCount():
                next_row = row_index + 1
                self._source_detail_next_pending = None
                self.source_table.setCurrentCell(next_row, 0)
                self.source_table.selectRow(next_row)
                self._source_detail_next_continuing = True
                try:
                    self._show_source_details(next_row, 0)
                finally:
                    self._source_detail_next_continuing = False
                return
            if not self._source_detail_has_more():
                self._update_source_detail_navigation()
                return
            if "source_page" in self._loading_kinds or "source_query" in self._loading_kinds:
                self._update_source_detail_navigation()
                return
            self._source_detail_navigation_generation += 1
            self._source_detail_next_pending = {
                "generation": self._source_detail_navigation_generation,
                "run_dir": self._run_dir,
                "anchor_ref_id": ref_id,
                "signature": self._source_detail_navigation_signature(),
            }
            self._update_source_detail_navigation()
            self._show_more_sources()
            if not self._source_page_mode:
                self._continue_source_detail_next()

        def _continue_source_detail_next(self):
            pending = self._source_detail_next_pending
            if pending is None:
                self._update_source_detail_navigation()
                return
            if (
                pending["generation"] != self._source_detail_navigation_generation
                or pending["run_dir"] != self._run_dir
                or pending["signature"] != self._source_detail_navigation_signature()
                or self._detail_dialog is None
                or str(self._detail_dialog[2].get("ref_id") or "")
                != pending["anchor_ref_id"]
            ):
                self._cancel_source_detail_next()
                return
            row_index = self._source_detail_row_index(pending["anchor_ref_id"])
            if row_index < 0:
                self._cancel_source_detail_next()
                return
            if row_index + 1 < self.source_table.rowCount():
                next_row = row_index + 1
                self._source_detail_next_pending = None
                self.source_table.setCurrentCell(next_row, 0)
                self.source_table.selectRow(next_row)
                self._source_detail_next_continuing = True
                try:
                    self._show_source_details(next_row, 0)
                finally:
                    self._source_detail_next_continuing = False
                return
            if not self._source_detail_has_more():
                self._cancel_source_detail_next()
                return
            if "source_page" in self._loading_kinds or "source_query" in self._loading_kinds:
                self._update_source_detail_navigation()
                return
            self._update_source_detail_navigation()
            self._show_more_sources()
            if not self._source_page_mode:
                self._continue_source_detail_next()

        def _show_source_details(self, row: int, _column: int):
            item = self.source_table.item(row, 0)
            payload = (
                item.data(QtCore.Qt.ItemDataRole.UserRole)
                if item is not None
                else None
            )
            if not isinstance(payload, Mapping):
                return
            if not self._source_detail_next_continuing:
                self._cancel_source_detail_next()
            if self._background_loading and source_detail_loader is not None and self._run_dir:
                if self._detail_dialog is not None:
                    if self._detail_dialog[2].get("ref_id") == payload.get("ref_id"):
                        show_overlay = getattr(
                            self, "_atlas_show_source_detail_overlay", None
                        )
                        if show_overlay is not None:
                            show_overlay()
                        self._update_source_detail_navigation()
                        return
                    self._detail_dialog[0].close()
                dialog = QtWidgets.QDialog(self._atlas_detail_host)
                dialog.setWindowFlags(QtCore.Qt.WindowType.Widget)
                dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
                dialog.setWindowTitle(str(payload.get("title") or text["details"]))
                dialog.resize(760, 540)
                layout = QtWidgets.QVBoxLayout(dialog)
                compact = bool(getattr(self, "_atlas_source_detail_compact", False))
                layout.setContentsMargins(
                    *((0, 0, 0, 0) if compact else (20, 16, 20, 24))
                )
                layout.setSpacing(0 if compact else 16)
                heading = QtWidgets.QWidget(dialog)
                heading.setObjectName("sourceDetailSummaryHeader")
                heading_layout = QtWidgets.QHBoxLayout(heading)
                heading_layout.setContentsMargins(0, 0, 0, 0)
                heading_layout.setSpacing(18)
                heading_title = QtWidgets.QLabel(
                    _source_summary_title(payload, text), heading,
                )
                heading_title.setObjectName("sourceDetailHeadingTitle")
                heading_title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                heading_title.setWordWrap(True)
                heading_title.setMinimumWidth(0)
                heading_title.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Ignored,
                    QtWidgets.QSizePolicy.Policy.Preferred,
                )
                heading_title.setAlignment(
                    QtCore.Qt.AlignmentFlag.AlignLeft
                    | QtCore.Qt.AlignmentFlag.AlignTop
                )
                heading_layout.addWidget(heading_title, 1)
                close = QtWidgets.QToolButton(heading)
                close.setObjectName("sourceDetailClose")
                close.setFixedSize(32, 32)
                close.setIconSize(QtCore.QSize(16, 16))
                close.setVisible(not compact)
                close.setAccessibleName(text.get("close", "Close"))
                close.setToolTip(close.accessibleName())
                close.setProperty("atlasIcon", "close")
                close.setProperty("atlasIconSize", 16)
                from .atlas_icons import icon
                close.setIcon(icon(QtCore, QtGui, "close"))
                close.clicked.connect(dialog.accept)
                heading_layout.addWidget(
                    close, 0, QtCore.Qt.AlignmentFlag.AlignTop,
                )
                self._sync_source_detail_heading_alignment(heading)
                heading.setVisible(not compact)
                layout.addWidget(heading)
                stack = QtWidgets.QStackedWidget(dialog)
                from .source_summary import SourceSummaryBrowser
                view = SourceSummaryBrowser(dialog)
                view.setObjectName("sourceDetailText")
                view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
                view.setViewportMargins(
                    *((24, 18, 24, 18) if getattr(
                        self, "_atlas_source_detail_compact", False
                    ) else (0, 0, 0, 0))
                )
                view.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
                view.setOpenExternalLinks(False)
                skeleton = self._skeleton_widget(dialog)
                stack.addWidget(view)
                stack.addWidget(skeleton)
                stack.setCurrentIndex(1)
                layout.addWidget(stack)
                view_row = dict(payload)
                self._connect_source_summary_links(view, view_row)
                detail_record = (dialog, view, view_row, stack)
                self._detail_dialog = detail_record
                animations = list(skeleton._atlas_loading_animations)

                def closed(_result=None, *, destroying=False):
                    if self._detail_dialog is not detail_record:
                        return
                    # A hidden WA_DeleteOnClose QDialog can be destroyed without
                    # finished(). Retire its record before callbacks consult it.
                    self._detail_dialog = None
                    self._skeleton_animations = [
                        animation for animation in self._skeleton_animations
                        if all(animation is not owned for owned in animations)
                    ]
                    if self._load_closed:
                        return
                    self._invalidate_load("detail")
                    if not self._source_detail_next_continuing:
                        self._cancel_source_detail_next()
                    if not destroying:
                        for animation in animations:
                            animation.stop()
                        view.clear()
                    self._atlas_detail_stack.setCurrentIndex(0)
                    reset_overlay = getattr(
                        self, "_atlas_reset_source_detail_overlay", None
                    )
                    if reset_overlay is not None and not self._source_detail_next_continuing:
                        reset_overlay()

                dialog.finished.connect(closed)
                dialog.destroyed.connect(lambda *_args: closed(destroying=True))
                self._atlas_detail_stack.addWidget(dialog)
                self._atlas_detail_stack.setCurrentWidget(dialog)
                dialog.show()
                show_overlay = getattr(
                    self, "_atlas_show_source_detail_overlay", None
                )
                if show_overlay is not None:
                    show_overlay()
                self._update_source_detail_navigation()
                run_dir, ref_id = self._run_dir, str(payload.get("ref_id") or "")
                self._queue_load("detail", lambda: source_detail_loader(run_dir, ref_id))
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(str(payload.get("title") or text["details"]))
            dialog.resize(760, 540)
            layout = QtWidgets.QVBoxLayout(dialog)
            from .source_summary import SourceSummaryBrowser
            view = SourceSummaryBrowser(dialog)
            view.setObjectName("sourceDetailText")
            view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            view.setViewportMargins(24, 18, 24, 18)
            view.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            view.setOpenExternalLinks(False)
            view_row = dict(payload)
            self._connect_source_summary_links(view, view_row)
            self._render_source_summary(view, view_row)
            layout.addWidget(view)
            close = QtWidgets.QPushButton("OK", dialog)
            close.clicked.connect(dialog.accept)
            layout.addWidget(close, 0, QtCore.Qt.AlignmentFlag.AlignRight)
            dialog.exec()

        def _refresh_static_tabs(self):
            if self._background_loading:
                self._request_page_data(self._page_index)
                return
            if self._page_index == 4:
                self._load_console_capture()
            self._apply_history_rows(history_loader() or ())
            self._apply_cache_rows(cache_loader() or ())
            self._apply_settings_payload(settings_loader() or {})
            self._render_documentation(docs_loader() or {"README.md": text["no_documentation"]})
            self._update_history_actions()
            self._update_run_controls()

        def _apply_history_rows(self, rows):
            self._history_rows = list(rows or ())
            available_run_dirs = {
                str(row.get("run_dir") or "") for row in self._history_rows
                if isinstance(row, Mapping) and self._history_row_is_selectable(row)
            }
            self._history_checked_run_dirs.intersection_update(available_run_dirs)
            self.history_table.blockSignals(True)
            if self._history_visible_count <= 0:
                self._history_visible_count = self._page_batch_size(self.history_table)
            self.history_more_button.setVisible(
                len(self._history_rows) < self._atlas_history_total if history_page_loader is not None
                else len(self._history_rows) > self._history_visible_count)
            self.history_table.setRowCount(min(len(self._history_rows), self._history_visible_count))
            self.history_stack.setCurrentIndex(0 if self.history_table.rowCount() else 2)
            for index, row in enumerate(self._history_rows[:self._history_visible_count]):
                counts = row.get("source_counts") or {}
                verification = row.get("verification_counts") or {}
                outcomes = verification.get("outcomes") or {}
                verdicts = [
                    f"{count} {text.get('atlas_outcome_' + name, name).lower()}"
                    for name, count in sorted(outcomes.items())
                ]
                if verification.get("open"):
                    verdicts.append(
                        f"{text['status_running']} {verification['open']}"
                    )
                verification_outcome = (
                    f"{text['verification']}: {' · '.join(verdicts)}"
                    if verification.get("total")
                    else text["verification_not_performed"]
                )
                verdict_summary = " · ".join(verdicts) if verification.get("total") else text["verification_not_performed"]
                run_outcome = " · ".join(
                    str(value) for value in (
                        row.get("phase"), row.get("run_status")
                    ) if value
                )
                outcome = row.get("outcome") or " | ".join(
                    value for value in (run_outcome, verification_outcome)
                    if value
                )
                coverage = row.get("coverage") or (
                    f"{text['resolved']} {counts.get('resolved', 0)} · "
                    f"Full text {counts.get('fulltext', 0)} · "
                    f"Abstract {counts.get('abstract', 0)} · "
                    f"{text['no_text']} {counts.get('none', 0)}"
                    if row.get("available", True)
                    else text["unavailable"]
                )
                if row.get("summary_pending"):
                    verdict_summary = coverage = text["loading_run"]
                elif row.get("summary_error"):
                    verdict_summary = coverage = text["load_failed"].format(
                        reason=row["summary_error"]
                    )
                complete = row.get("complete")
                if isinstance(complete, bool):
                    complete = text["yes"] if complete else text["no"]
                elif complete is None:
                    complete = (
                        text["yes"] if row.get("completed") else text["no"]
                    )
                if not row.get("available", True):
                    unavailable = (
                        text["history_schema_unavailable"]
                        if row.get("unavailable_reason") == "incompatible_schema"
                        else text["unavailable"]
                    )
                    outcome = unavailable
                    verdict_summary = unavailable
                run_dir = str(row.get("run_dir") or "")
                checked = run_dir in self._history_checked_run_dirs
                select_item = QtWidgets.QTableWidgetItem()
                select_item.setData(QtCore.Qt.ItemDataRole.UserRole, dict(row))
                select_flags = QtCore.Qt.ItemFlag.ItemIsEnabled
                if self._history_row_is_selectable(row):
                    select_flags |= QtCore.Qt.ItemFlag.ItemIsUserCheckable
                select_item.setFlags(select_flags)
                select_item.setCheckState(
                    QtCore.Qt.CheckState.Checked if checked
                    else QtCore.Qt.CheckState.Unchecked
                )
                self.history_table.setItem(index, 0, select_item)
                for column, value in enumerate((row.get("paper") or row.get("input") or text["unavailable"], verdict_summary, outcome, coverage, complete), 1):
                    item = QtWidgets.QTableWidgetItem(str(value))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, dict(row))
                    self.history_table.setItem(index, column, item)
            self.history_table.blockSignals(False)
            self._sync_history_select_all()
            self._update_history_actions()
            self._update_run_controls()
            self._refresh_collection_presentation()

        def _apply_cache_rows(self, cache_payload):
            if isinstance(cache_payload, Mapping):
                self._cache_rows = list(cache_payload.get("items") or ())
            else:
                self._cache_rows = list(cache_payload)
            current_cache_keys = {
                self._cache_row_key(row) for row in self._cache_rows
                if isinstance(row, Mapping)
            }
            self._cache_checked_keys.intersection_update(current_cache_keys)
            fulltext_count = sum(
                1 for row in self._cache_rows
                if (row.get("tier") or row.get("availability")) == "fulltext"
            )
            abstract_count = sum(
                1 for row in self._cache_rows
                if (row.get("tier") or row.get("availability")) == "abstract"
            )
            total_chars = sum(
                int(content.get("char_count") or 0)
                for row in self._cache_rows
                for content in row.get("contents") or ()
            )
            self.cache_summary.setText(
                f"{len(self._cache_rows)} · {text['fulltext']} {fulltext_count} · "
                f"{text['abstract']} {abstract_count} · "
                f"{total_chars:,} {text['characters']}"
            )
            self._render_cache()

        def _apply_settings_payload(self, settings_payload):
            if not self._settings_loaded or not self._settings_dirty:
                staged = dict(self._settings_dirty)
                replacing = set(self._settings_replacing)
                if isinstance(settings_payload, Mapping):
                    settings = [{"group": "advanced", "label": key, "name": key,
                                 "control": "secret" if _is_secret_setting_name(str(key)) else "text",
                                 "configured": bool(value), "value": "" if _is_secret_setting_name(str(key)) else value,
                                 "source": ""}
                                for key, value in sorted(settings_payload.items())]
                else:
                    settings = list(settings_payload)
                self._render_settings(settings)
                if staged or replacing:
                    self._settings_dirty.update(staged)
                    self._settings_replacing.update(replacing)
                    self._render_settings_category(
                        self._settings_categories_key(self.settings_categories.currentRow())
                    )

        def _request_page(self, index: int):
            if index == 4 and not self._console_enabled:
                self.navigation.blockSignals(True)
                self.navigation.setCurrentRow(self._page_index)
                self.navigation.blockSignals(False)
                return
            previous_index = self._page_index
            if index != self._page_index and self._page_index == 3 and self._settings_dirty:
                decision = self._confirm_settings_changes()
                if decision == "cancel":
                    self.navigation.blockSignals(True)
                    self.navigation.setCurrentRow(self._page_index)
                    self.navigation.blockSignals(False)
                    return
                if decision == "save" and not self._save_settings():
                    self.navigation.blockSignals(True)
                    self.navigation.setCurrentRow(self._page_index)
                    self.navigation.blockSignals(False)
                    return
                if decision == "discard":
                    self._discard_settings()
            if index != previous_index:
                self._release_page_data(previous_index)
            self._page_index = index
            self.pages.setCurrentIndex(index)
            controller = getattr(self, "_atlas_navigation_controller", None)
            if controller is not None:
                controller.close_overlay()
            if previous_index != index:
                from .atlante import fade_in
                fade_in(self.pages.currentWidget(), QtCore, QtWidgets)
            if index == 0:
                self._refresh_llm_choices()
                self._refresh_llm_status()
                if self._background_loading:
                    self._request_page_data(index)
            else:
                self._refresh_static_tabs()

        def _settings_category(self, row: Mapping[str, Any]) -> str:
            from .atlante import settings_category
            return settings_category(row)

        def _build_settings_search_index(self):
            from core.gui.settings.services import (
                SEARCH_PRESETS,
                group_service_rows,
                normalize_settings_search,
            )

            categories = ("general", "resolution", "fetch", "personalization")
            category_labels = {
                category: text.get("group_" + category, category.title())
                for category in categories
            }
            service_labels = {}
            groups, _remaining = group_service_rows(self._settings_rows, text)
            for group_id, _icon_key, service_rows in groups:
                label = text.get("service_group_" + group_id, group_id)
                for row in service_rows:
                    service_labels[str(row.get("name") or "")] = label

            entries = []

            def add_entry(*, name, category, title, summary="", help_text="",
                          provider="", provider_id="", kind="setting", target=None,
                          extra=(), secret=False):
                parts = [name, title, summary, help_text, provider, provider_id,
                         category_labels.get(category, category), *extra]
                if secret:
                    parts.append(text.get("settings_search_secret_terms", "API key token credential"))
                search_text = normalize_settings_search(" ".join(
                    str(part) for part in parts if part
                ))
                if not search_text.strip():
                    return
                entries.append({
                    "name": str(name), "category": str(category), "title": str(title),
                    "summary": str(summary), "provider": str(provider),
                    "provider_id": str(provider_id),
                    "kind": str(kind), "target": str(target or name),
                    "search_text": search_text,
                })

            guided_url = "CITATION_VERIFIER_GUIDED_SEARCH_URL"
            guided_query = "CITATION_VERIFIER_GUIDED_SEARCH_QUERY"
            for row in self._settings_rows:
                name = str(row.get("name") or "")
                category = self._settings_category(row)
                if not name or category not in categories:
                    continue
                if name in {guided_url, guided_query}:
                    continue
                label = str(
                    text.get(f"setting_{name}_label")
                    or row.get("label")
                    or text.get("setting_custom_label", "Additional setting")
                )
                summary = str(text.get(f"setting_{name}_summary") or "")
                help_text = str(text.get(f"setting_{name}_help") or "")
                provider = str(row.get("provider") or "")
                extras = (service_labels.get(name, ""),)
                add_entry(
                    name=name, category=category, title=label, summary=summary,
                    help_text=help_text, provider=provider, target=name, extra=extras,
                    secret=(row.get("control") == "secret" or bool(row.get("secret"))),
                )

            # Provider API credentials and endpoints are visible in the Jury
            # chooser, although they are deliberately absent from the main
            # settings categories. Reuse its existing provider classification.
            from .jury_chooser import (
                _provider_configuration_row,
                _provider_label,
                _setting_provider_ids,
            )
            for row in self._settings_rows:
                if str(row.get("group") or "").lower() != "models":
                    continue
                name = str(row.get("name") or "")
                if not name:
                    continue
                secret = row.get("control") == "secret" or bool(row.get("secret"))
                for provider_id in sorted(_setting_provider_ids(row)):
                    if not _provider_configuration_row(row, provider_id):
                        continue
                    provider_label = _provider_label(self, provider_id)
                    endpoint = name.endswith(("_BASE_URL", "_HOST"))
                    title = text.get(
                        "jury_endpoint_label" if endpoint else "jury_api_key_label",
                        "Endpoint" if endpoint else "API key",
                    )
                    summary = str(text.get(f"setting_{name}_summary") or "")
                    help_text = str(text.get(f"setting_{name}_help") or "")
                    add_entry(
                        name=name, category="models", title=f"{provider_label} · {title}",
                        summary=summary, help_text=help_text, provider=provider_label,
                        provider_id=provider_id,
                        kind="provider", target=name,
                        extra=(text.get("group_models", "Models and juries"),),
                        secret=secret,
                    )

            def custom_search_fields_visible():
                current_url = self._settings_dirty.get(
                    guided_url,
                    self._settings_initial.get(guided_url, SEARCH_PRESETS[0][2]),
                )
                current_query = self._settings_dirty.get(
                    guided_query,
                    self._settings_initial.get(guided_query, "{raw_entry}"),
                )
                preset_urls = {preset[2] for preset in SEARCH_PRESETS}
                return not (current_url in preset_urls and current_query == "{raw_entry}")

            if custom_search_fields_visible():
                for name, label_key, summary_key, help_key, extra in (
                    (guided_url, "setting_CITATION_VERIFIER_GUIDED_SEARCH_URL_label",
                     "setting_CITATION_VERIFIER_GUIDED_SEARCH_URL_summary",
                     "setting_CITATION_VERIFIER_GUIDED_SEARCH_URL_help", ("URL address search" ,)),
                    (guided_query, "setting_CITATION_VERIFIER_GUIDED_SEARCH_QUERY_label",
                     "setting_CITATION_VERIFIER_GUIDED_SEARCH_QUERY_summary",
                     "setting_CITATION_VERIFIER_GUIDED_SEARCH_QUERY_help", ("query terms template" ,)),
                ):
                    add_entry(
                        name=name, category="fetch", title=text.get(label_key, name),
                        summary=text.get(summary_key, ""), help_text=text.get(help_key, ""),
                        target=name, extra=extra,
                    )

            # These controls are built outside the generic settings-row form.
            manual_controls = (
                ("__application_language__", "general", "application_language",
                 "setting_application_language_summary", "setting_application_language_help",
                 "applicationLanguage", ("language lingua" ,)),
                ("appearanceTheme", "personalization", "appearance_theme", "", "",
                 "appearanceTheme", ("theme tema color colore dark scuro light chiaro" ,)),
                ("appearanceFont", "personalization", "appearance_font", "", "",
                 "appearanceFont", ("font size text dimensione carattere" ,)),
                ("enableCliConsole", "personalization", "enable_cli_console", "", "",
                 "enableCliConsole", ("cli console command line terminal" ,)),
                ("__signing_key__", "general", "signing_key_label",
                 "signing_key_description", "signing_key_description",
                 "signingKeyAction", ("HMAC audit report seal signature chiave di firma" ,)),
                ("guidedSearchProvider", "fetch", "search_engine", "", "",
                 "guidedSearchProvider", ("search engine motore ricerca" ,)),
                ("browserCachePolicy", "fetch", "browser_cache_cleanup", "",
                 "browser_cache_help", "browserCachePolicy",
                 ("browser cache policy automatic cleanup pulizia" ,)),
                ("clearBrowserCache", "fetch", "browser_cache_clear", "",
                 "browser_cache_help", "clearBrowserCache",
                 ("browser cache clear now svuota" ,)),
            )
            for name, category, label_key, summary_key, help_key, target, extra in manual_controls:
                add_entry(
                    name=name, category=category, title=text.get(label_key, name),
                    summary=text.get(summary_key, "") if summary_key else "",
                    help_text=text.get(help_key, "") if help_key else "",
                    target=target, extra=extra,
                )

            category_order = {name: index for index, name in enumerate(categories)}
            category_order["models"] = len(categories)
            entries.sort(key=lambda entry: (
                category_order.get(entry["category"], len(categories) + 1),
                normalize_settings_search(entry["title"]),
                entry["name"].casefold(), entry["provider_id"].casefold(),
                entry["provider"].casefold(),
                entry["kind"],
            ))
            self._settings_search_index = tuple(entries)

        def _settings_search_page_changed(self, index):
            if self.pages.widget(index) is not getattr(self, "_settings_page", None):
                self._hide_settings_search_results()

        def _hide_settings_search_results(self):
            results = getattr(self, "settings_search_results", None)
            if results is not None:
                results.hide()
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)

        def _show_settings_search_results(self):
            results = getattr(self, "settings_search_results", None)
            if results is None:
                return
            results.show()
            results.raise_()
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
            self._schedule_settings_search_item_sizes()

        def _is_settings_search_control(self, widget):
            while isinstance(widget, QtWidgets.QWidget):
                if widget in (
                    getattr(self, "settings_search", None),
                    getattr(self, "settings_search_results", None),
                ):
                    return True
                widget = widget.parentWidget()
            return False

        def _refresh_settings_search_results(self, query=None):
            if not hasattr(self, "settings_search_results"):
                return
            from core.gui.settings.services import search_settings

            if query is None:
                query = self.settings_search.text()
            self.settings_search_results.clear()
            if not str(query).strip():
                self._hide_settings_search_results()
                return
            matches = search_settings(
                getattr(self, "_settings_search_index", ()), str(query)
            )
            if not matches:
                item = QtWidgets.QListWidgetItem(text["settings_search_no_results"])
                item.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
                self.settings_search_results.addItem(item)
            else:
                for entry in matches:
                    item = QtWidgets.QListWidgetItem(entry["title"])
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, dict(entry))
                    self.settings_search_results.addItem(item)
            if self.pages.currentWidget() is not getattr(self, "_settings_page", None):
                self._hide_settings_search_results()
                return
            self._show_settings_search_results()

        def _schedule_settings_search_item_sizes(self):
            if self._settings_search_size_update_pending:
                return
            self._settings_search_size_update_pending = True
            QtCore.QTimer.singleShot(0, self._resize_settings_search_items)

        def _resize_settings_search_items(self):
            self._settings_search_size_update_pending = False
            self._reflow_settings_header()
            results = getattr(self, "settings_search_results", None)
            search = getattr(self, "settings_search", None)
            page = getattr(self, "_settings_page", None)
            description = getattr(self, "settings_description", None)
            if search is None or page is None or description is None:
                return
            margins = description.contentsMargins()
            top_margin = max(
                0, (search.height() - description.fontMetrics().height()) // 2
            )
            if margins.top() != top_margin:
                description.setContentsMargins(
                    margins.left(), top_margin, margins.right(), margins.bottom()
                )
            if results is None or results.count() == 0:
                return
            width = max(180, search.width() - 34)
            flags = int(QtCore.Qt.TextFlag.TextWordWrap)
            metrics = results.fontMetrics()
            for index in range(results.count()):
                item = results.item(index)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) is None:
                    continue
                bounds = metrics.boundingRect(
                    QtCore.QRect(0, 0, width, 2000), flags, item.text(),
                )
                item.setSizeHint(QtCore.QSize(width, max(48, bounds.height() + 14)))
            results.doItemsLayout()
            height = min(
                176, 2 * results.frameWidth()
                + sum(results.sizeHintForRow(row) for row in range(results.count())),
            )
            results.setFixedHeight(height)
            position = search.mapTo(page, QtCore.QPoint(0, search.height() + 4))
            results.setGeometry(position.x(), position.y(), search.width(), height)
            if not results.isHidden():
                results.raise_()

        def _reflow_settings_header(self):
            page = getattr(self, "_settings_page", None)
            if page is None:
                return
            compact = page.width() < 640 or page.height() < 480
            self._align_browser_cache_action(compact)
            changed = compact != self._settings_header_compact
            if changed:
                self._settings_header_compact = compact
                root = page.layout()
                row = self._settings_description_layout
                toggle = self.settings_navigation_toggle
                self.settings_description.setVisible(not compact)
                if compact:
                    root.removeWidget(toggle)
                    row.insertWidget(0, toggle, 0, QtCore.Qt.AlignmentFlag.AlignTop)
                    row.setSpacing(10)
                    root.setContentsMargins(8, 8, 8, 0)
                    root.setSpacing(8)
                    self._settings_search_panel.setMinimumWidth(160)
                else:
                    row.removeWidget(toggle)
                    root.insertWidget(2, toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
                    row.setSpacing(20)
                    root.setContentsMargins(30, 26, 30, 0)
                    root.setSpacing(18)
                    self._settings_search_panel.setMinimumWidth(240)

            settings_layout = getattr(self, "settings_layout", None)
            settings_content = getattr(self, "settings_content", None)
            if settings_layout is not None and settings_content is not None:
                settings_layout.setContentsMargins(
                    0, 0, 0, 0
                ) if compact else settings_layout.setContentsMargins(
                    *self._settings_wide_content_margins
                )
                for section in settings_content.findChildren(
                    QtWidgets.QFrame, "settingsSection"
                ):
                    layout = section.layout()
                    if layout is None:
                        continue
                    if not hasattr(section, "_settings_wide_layout_margins"):
                        margins = layout.contentsMargins()
                        section._settings_wide_layout_margins = (
                            margins.left(), margins.top(),
                            margins.right(), margins.bottom(),
                        )
                    if compact:
                        top_gap = 20 if section.property("compactSectionBoundary") else 0
                        layout.setContentsMargins(0, top_gap, 0, 0)
                    else:
                        layout.setContentsMargins(
                            *section._settings_wide_layout_margins
                        )
                    if section.property("compactSettings") != compact:
                        section.setProperty("compactSettings", compact)
                        section.style().unpolish(section)
                        section.style().polish(section)
                        section.updateGeometry()
            controller = getattr(self, "_settings_navigation_controller", None)
            if changed and controller is not None:
                controller.sync()

        def _activate_first_settings_search_result(self):
            for index in range(self.settings_search_results.count()):
                item = self.settings_search_results.item(index)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) is not None:
                    self._activate_settings_search_item(item)
                    return

        def _activate_settings_search_item(self, item, *_args):
            if item is None:
                return
            entry = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(entry, Mapping):
                return
            if entry.get("kind") == "provider":
                if self._process is not None:
                    return
                self._hide_settings_search_results()
                provider_id = str(entry.get("provider_id") or "")
                role = getattr(self, "_llm_picker_role", "jury1")
                if role not in {"jury1", "jury2"}:
                    role = "jury1"
                self._show_jury_models(role)
                if self._process is not None:
                    return
                if provider_id:
                    self._atlante_jury_chooser.show_provider(provider_id)
                    QtCore.QTimer.singleShot(
                        0,
                        lambda p=provider_id, n=str(entry.get("name") or ""):
                        self._focus_jury_search_target(p, n),
                    )
                return

            category = str(entry.get("category") or "")
            target_name = str(entry.get("target") or entry.get("name") or "")
            category_row = next((
                index for index in range(self.settings_categories.count())
                if self.settings_categories.item(index).data(QtCore.Qt.ItemDataRole.UserRole)
                == category
            ), -1)
            if category_row < 0:
                return
            self._hide_settings_search_results()
            if self.settings_categories.currentRow() != category_row:
                self.settings_categories.setCurrentRow(category_row)
            QtCore.QTimer.singleShot(
                0, lambda c=category, n=target_name:
                self._focus_settings_search_target(c, n),
            )

        def _focus_settings_search_target(self, category, name):
            if self._settings_categories_key(self.settings_categories.currentRow()) != category:
                return
            target = getattr(self, "_settings_search_targets", {}).get(name)
            if target is None:
                return
            self._settings_search_focus = (category, name)
            self._reveal_settings_search_control(self.settings_scroll, target)

        def _schedule_settings_search_reveal(self, *_args):
            if self._settings_search_focus is None or self._settings_search_reveal_pending:
                return
            self._settings_search_reveal_pending = True
            QtCore.QTimer.singleShot(0, self._reveal_focused_settings_search_target)

        def _settings_search_scroll_value_changed(self, value):
            previous_value = self._settings_search_last_scroll_value
            self._settings_search_last_scroll_value = value
            if self._settings_search_reveal_applying or self._settings_search_focus is None:
                return
            scrollbar = self.settings_scroll.verticalScrollBar()
            layout_clamped = (
                value == scrollbar.minimum() and previous_value < scrollbar.minimum()
            ) or (
                value == scrollbar.maximum() and previous_value > scrollbar.maximum()
            )
            if not layout_clamped:
                # A moved scroll position is the user's choice. Keep the editor
                # focused, but stop later range changes from pulling it back.
                self._settings_search_focus = None
                self._settings_search_reveal_pending = False

        def _reveal_focused_settings_search_target(self):
            self._settings_search_reveal_pending = False
            if self._settings_search_focus is None:
                return
            category, name = self._settings_search_focus
            if self._settings_categories_key(self.settings_categories.currentRow()) != category:
                return
            target = self._settings_search_targets.get(name)
            # Late form reflow changes the scroll range after the first reveal.
            # Reacquire the current control, and never steal focus back.
            if target is not None and target.hasFocus():
                self._reveal_settings_search_control(self.settings_scroll, target)

        def _reveal_settings_search_control(self, scroll, target):
            is_settings_scroll = scroll is getattr(self, "settings_scroll", None)
            was_revealing = self._settings_search_reveal_applying
            if is_settings_scroll:
                self._settings_search_reveal_applying = True
            try:
                target.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
                scroll.ensureWidgetVisible(target, 4, 12)
                top_left = target.mapTo(scroll.widget(), QtCore.QPoint())
                bottom_right = top_left + target.rect().bottomRight()
                # ensureWidgetVisible can use an editor's caret rectangle; reveal
                # both control corners so its border stays above the sticky footer.
                scroll.ensureVisible(top_left.x(), top_left.y(), 4, 12)
                scroll.ensureVisible(bottom_right.x(), bottom_right.y(), 4, 12)
            finally:
                if is_settings_scroll:
                    self._settings_search_reveal_applying = was_revealing
                    self._settings_search_last_scroll_value = scroll.verticalScrollBar().value()

        def _focus_jury_search_target(self, provider, name):
            chooser = getattr(self, "_atlante_jury_chooser", None)
            if chooser is None or chooser.current_provider() != provider:
                return
            target = getattr(chooser, "_chooser_editors", {}).get(name)
            if target is None:
                target = self._settings_editors.get(name)
            if target is None:
                return
            self._reveal_settings_search_control(chooser.content_scroll, target)

        def _render_settings(self, settings):
            self._settings_rows = [row for row in settings if isinstance(row, Mapping)]
            if not any(row.get("name") == "CITATION_VERIFIER_MAILTO" for row in self._settings_rows):
                self._settings_rows.append({
                    "name": "CITATION_VERIFIER_MAILTO", "group": "general",
                    "control": "text", "value": "", "configured": False,
                })
            self._settings_initial = {
                str(row.get("name") or ""): str(row.get("value") or "")
                for row in self._settings_rows if row.get("name")
            }
            self._settings_dirty.clear()
            self._settings_replacing.clear()
            self._settings_loaded = True
            if getattr(self, "_settings_transfer_restore_pending", False):
                self._settings_transfer_restore_pending = False
                accuracy = self._settings_initial.get("CITATION_VERIFIER_ACCURACY", "standard")
                if accuracy in self.mode_buttons:
                    self.mode_buttons[accuracy].setChecked(True)
                level = self._settings_initial.get("CITATION_VERIFIER_VERIFY_JURY2_LEVEL", "medium")
                for button in self.jury_buttons:
                    if button.property("jury2_level") == level:
                        button.setChecked(True)
                self._refresh_llm_status()
            categories = ("general", "resolution", "fetch", "personalization")
            current = self.settings_categories.currentRow()
            self.settings_categories.blockSignals(True)
            self.settings_categories.clear()
            for category in categories:
                item = QtWidgets.QListWidgetItem(text.get("group_" + category, category.title()))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, category)
                from .atlas_icons import icon
                item.setIcon(icon(
                    QtCore, QtGui,
                    {"general": "settings", "resolution": "search", "fetch": "download",
                     "personalization": "palette"}[category],
                    "#afbfdb" if self._dark else "#455374",
                ))
                self.settings_categories.addItem(item)
            self.settings_categories.blockSignals(False)
            self.settings_categories.setCurrentRow(max(0, min(current, self.settings_categories.count() - 1)))
            self._select_settings_category(self.settings_categories.currentRow())
            if hasattr(self, "_atlante_jury_chooser"):
                self._atlante_jury_chooser.refresh_settings()
            self._build_settings_search_index()
            self._refresh_settings_search_results()

        def _select_settings_category(self, index: int):
            if index < 0:
                return
            self._render_settings_category(self._settings_categories_key(index))
            controller = getattr(self, "_settings_navigation_controller", None)
            if controller is not None:
                controller.close_overlay()

        def _settings_categories_key(self, index: int) -> str:
            item = self.settings_categories.item(index)
            if item is None:
                return "advanced"
            category = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if category:
                return str(category)
            reverse = {text.get("group_" + key, key.title()): key for key in
                       ("general", "resolution", "fetch", "personalization")}
            return reverse.get(item.text(), "advanced")

        def _render_settings_category(self, category: str):
            self.network_permissions_button = None
            self._settings_transfer_row = None
            self._browser_cache_status = None
            self._browser_cache_clear_button = None
            while self.settings_layout.count():
                child = self.settings_layout.takeAt(0)
                if child.widget() is not None:
                    child.widget().hide()
                    child.widget().deleteLater()
                elif child.layout() is not None:
                    self._clear_layout(child.layout())
            self._settings_editors = {}
            self._secret_editors = {}
            self._settings_search_targets = {}
            self._provider_check_controls = {}
            self._provider_model_editors = {}
            self.signing_key_button = None
            self.signing_key_status = None
            if category == "general":
                card = QtWidgets.QFrame(self.settings_content); card.setObjectName("settingsSection")
                form = QtWidgets.QFormLayout(card)
                form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
                form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                label_text = text["application_language"]
                field = QtWidgets.QWidget(card)
                field_layout = QtWidgets.QVBoxLayout(field)
                field_layout.setContentsMargins(0, 0, 0, 0)
                language_selector = self._language_selector(field)
                field_layout.addWidget(language_selector)
                self._settings_search_targets["__application_language__"] = language_selector
                note = QtWidgets.QLabel(
                    text.get("setting_application_language_summary", text["setting_unknown_summary"]),
                    field,
                )
                note.setObjectName("settingNote")
                note.setProperty("settingName", "__application_language__")
                note.setWordWrap(True)
                self._add_setting_row(
                    form, "__application_language__", label_text,
                    text["setting_application_language_help"], field, note,
                )
                network_field = QtWidgets.QWidget(card)
                network_field_layout = QtWidgets.QVBoxLayout(network_field)
                network_field_layout.setContentsMargins(0, 0, 0, 0)
                self.network_permissions_button = QtWidgets.QPushButton(
                    text["network_permissions_manage"], network_field,
                )
                self.network_permissions_button.setObjectName("networkPermissionsButton")
                self.network_permissions_button.setToolTip(
                    text["network_permissions_summary"]
                )
                self.network_permissions_button.clicked.connect(
                    self._manage_network_permissions
                )
                network_field_layout.addWidget(
                    self.network_permissions_button,
                    0,
                    QtCore.Qt.AlignmentFlag.AlignLeft,
                )
                network_note = QtWidgets.QLabel(
                    text["network_permissions_summary"], network_field,
                )
                network_note.setObjectName("settingNote")
                network_note.setProperty("settingName", "__network_permissions__")
                network_note.setWordWrap(True)
                self._add_setting_row(
                    form, "__network_permissions__",
                    text["network_permissions_label"],
                    text["network_permissions_summary"],
                    network_field, network_note,
                )
                self.settings_layout.addWidget(card)
            rows = [row for row in self._settings_rows if self._settings_category(row) == category]
            if category == "resolution":
                from core.gui.settings.services import group_service_rows
                from .atlas_icons import icon
                service_groups, remaining = group_service_rows(rows, text)
                for group_id, icon_key, service_rows in service_groups:
                    card = QtWidgets.QFrame(self.settings_content)
                    card.setObjectName("settingsSection")
                    card.setProperty("serviceGroup", group_id)
                    form = QtWidgets.QFormLayout(card)
                    form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
                    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                    heading = QtWidgets.QWidget(card)
                    heading_layout = QtWidgets.QHBoxLayout(heading)
                    heading_layout.setContentsMargins(0, 0, 0, 0)
                    mark = QtWidgets.QLabel(heading)
                    mark.setObjectName("serviceGroupIcon")
                    mark.setProperty("atlasIcon", icon_key)
                    ink = "#afbfdb" if self._dark else "#455374"
                    mark.setPixmap(icon(QtCore, QtGui, icon_key, ink, 22).pixmap(22, 22))
                    heading_layout.addWidget(mark)
                    label = QtWidgets.QLabel(text[f"service_group_{group_id}"], heading)
                    label.setObjectName("sectionTitle")
                    heading_layout.addWidget(label, 1)
                    form.addRow(heading)
                    for row in service_rows:
                        self._add_setting_editor(form, card, row)
                    if provider_check_callback is not None:
                        provider_ids = {
                            str(row.get("check_provider") or "") for row in service_rows
                        } - {""}
                        for provider_id in sorted(provider_ids):
                            self._add_provider_check_control(form, card, provider_id)
                    self.settings_layout.addWidget(card)
                rows = list(remaining)
            providers: dict[str, list[Mapping[str, Any]]] = {}
            for row in rows:
                providers.setdefault(str(row.get("provider") or ""), []).append(row)
            provider_order = {
                "Codex CLI": (0, 0), "Codex": (0, 0),
                "Claude Code": (1, 0),
                "OpenAI / compatible": (2, 0), "OpenAI compatible": (2, 0),
                "OpenAI": (2, 1),
            }
            provider_groups = sorted(providers.items(), key=lambda item: (
                *provider_order.get(item[0], (3, 0)), item[0].casefold(),
            )) if category == "models" else providers.items()
            for provider, provider_rows in provider_groups:
                card = QtWidgets.QFrame(self.settings_content); card.setObjectName("settingsSection")
                form = QtWidgets.QFormLayout(card)
                form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
                form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                if category == "models" and provider:
                    heading = QtWidgets.QLabel(provider, card); heading.setObjectName("sectionTitle")
                    form.addRow(heading, QtWidgets.QLabel("", card))
                for row in provider_rows:
                    if row.get("name") in {"CITATION_VERIFIER_GUIDED_SEARCH_URL", "CITATION_VERIFIER_GUIDED_SEARCH_QUERY"}:
                        continue
                    self._add_setting_editor(form, card, row)
                if provider_check_callback is not None:
                    provider_ids = {
                        str(row.get("check_provider") or "")
                        for row in provider_rows
                    } - {""}
                    for provider_id in provider_ids:
                        self._add_provider_check_control(form, card, provider_id)
                self.settings_layout.addWidget(card)
            if category == "fetch":
                self._add_search_settings()
                self._add_browser_cache_settings()
            if category == "general":
                self._add_signing_key_settings()
                self._add_settings_transfer_controls()
            if category == "personalization":
                self._add_personalization_settings()
            if category != "general" and category != "personalization" and not rows and not (category == "resolution" and service_groups):
                empty = QtWidgets.QLabel(text["settings_empty"], self.settings_content)
                empty.setWordWrap(True); self.settings_layout.addWidget(empty)
            self.settings_layout.addStretch()
            self._reflow_settings_header()
            self._schedule_settings_caption_alignment()

        def _settings_caption_control(self, field):
            if field is None:
                return None
            control_types = (
                QtWidgets.QComboBox,
                QtWidgets.QLineEdit,
                QtWidgets.QAbstractSpinBox,
                QtWidgets.QTextEdit,
                QtWidgets.QPlainTextEdit,
                QtWidgets.QAbstractButton,
                QtWidgets.QSlider,
            )
            candidates = []
            widgets = [field, *field.findChildren(QtWidgets.QWidget)]
            for priority, control_type in enumerate(control_types):
                candidates.extend(
                    (priority, widget)
                    for widget in widgets
                    if isinstance(widget, control_type)
                )
            if candidates:
                return min(candidates, key=lambda candidate: candidate[0])[1]
            return field if isinstance(field, QtWidgets.QWidget) else None

        def _watch_settings_caption_geometry(self, widget):
            if not isinstance(widget, QtWidgets.QWidget):
                return
            try:
                if getattr(widget, "_settings_caption_alignment_watched", False):
                    return
                widget._settings_caption_alignment_watched = True
                widget.installEventFilter(self)
            except RuntimeError:
                return

        def _align_settings_caption(self, label, field):
            try:
                if label is None or field is None or not label.objectName().startswith("settingCaption"):
                    return
                control = self._settings_caption_control(field)
                if control is None:
                    return
                self._watch_settings_caption_geometry(field)
                self._watch_settings_caption_geometry(control)
                label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
                line_height = label.fontMetrics().height()
                control_height = control.height() or control.sizeHint().height()
                top_margin = max(0, (control_height - line_height) // 2)

                ancestors = set()
                current = label.parentWidget()
                while current is not None:
                    ancestors.add(current)
                    current = current.parentWidget()
                common_parent = control.parentWidget()
                while common_parent is not None and common_parent not in ancestors:
                    common_parent = common_parent.parentWidget()
                if (
                    common_parent is not None
                    and label.isVisible()
                    and control.isVisible()
                    and label.width() > 0
                    and control.width() > 0
                ):
                    label_pos = label.mapTo(common_parent, QtCore.QPoint(0, 0))
                    field_pos = field.mapTo(common_parent, QtCore.QPoint(0, 0))
                    control_pos = control.mapTo(common_parent, QtCore.QPoint(0, 0))
                    side_by_side = field_pos.x() >= label_pos.x() + label.width() - 1
                    if side_by_side:
                        target_top = control_pos.y() + (control.height() - line_height) / 2
                        top_margin = max(0, int(round(target_top - label_pos.y())))
                    else:
                        top_margin = 0

                margins = label.contentsMargins()
                if margins.top() != top_margin:
                    label.setContentsMargins(
                        margins.left(), top_margin, margins.right(), margins.bottom()
                    )
            except RuntimeError:
                return

        def _schedule_settings_caption_alignment(self):
            if getattr(self, "_settings_caption_alignment_pending", False):
                return
            if not hasattr(self, "settings_content"):
                return
            self._settings_caption_alignment_pending = True
            QtCore.QTimer.singleShot(0, self._align_settings_caption_columns)

        def _align_settings_caption_columns(self):
            self._settings_caption_alignment_pending = False
            if not hasattr(self, "settings_content"):
                return

            transfer_row = getattr(self, "_settings_transfer_row", None)
            if transfer_row is not None:
                buttons = [
                    item.widget() for index in range(transfer_row.count())
                    if isinstance(
                        (item := transfer_row.itemAt(index)).widget(),
                        QtWidgets.QPushButton,
                    )
                ]
                required = (
                    sum(button.sizeHint().width() for button in buttons)
                    + max(0, transfer_row.spacing())
                    * max(0, transfer_row.count() - 1)
                    + 12
                )
                compact = self._settings_scroll_viewport.width() - 72 < required
                gap_index = getattr(self, "_settings_transfer_reset_gap_index", -1)
                gap_item = transfer_row.itemAt(gap_index) if gap_index >= 0 else None
                gap = gap_item.spacerItem() if gap_item is not None else None
                if gap is not None:
                    if compact:
                        gap.changeSize(
                            0, 0,
                            QtWidgets.QSizePolicy.Policy.Minimum,
                            QtWidgets.QSizePolicy.Policy.Minimum,
                        )
                    else:
                        gap.changeSize(
                            12, 0,
                            QtWidgets.QSizePolicy.Policy.Fixed,
                            QtWidgets.QSizePolicy.Policy.Minimum,
                        )
                transfer_row.setDirection(
                    QtWidgets.QBoxLayout.Direction.TopToBottom
                    if compact
                    else QtWidgets.QBoxLayout.Direction.LeftToRight
                )
                transfer_row.invalidate()

            cards = [
                item.widget()
                for index in range(self.settings_layout.count())
                if (item := self.settings_layout.itemAt(index)).widget() is not None
            ]
            forms = []
            for card in cards:
                forms.extend(card.findChildren(QtWidgets.QFormLayout))
                if isinstance(card.layout(), QtWidgets.QFormLayout):
                    forms.append(card.layout())
            labels = []
            natural_width = 0
            active_forms = list(dict.fromkeys(forms))
            for form in active_forms:
                form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
                form.setFieldGrowthPolicy(
                    QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
                )
                form.setLabelAlignment(
                    QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
                )
                for row_index in range(form.rowCount()):
                    label_item = form.itemAt(
                        row_index, QtWidgets.QFormLayout.ItemRole.LabelRole
                    )
                    field_item = form.itemAt(
                        row_index, QtWidgets.QFormLayout.ItemRole.FieldRole
                    )
                    label = label_item.widget() if label_item is not None else None
                    field = field_item.widget() if field_item is not None else None
                    if not isinstance(label, QtWidgets.QLabel) or field is None:
                        continue
                    if label.objectName() == "sectionTitle":
                        continue
                    if label.text().strip():
                        label.setObjectName("settingCaption")
                        label.setWordWrap(True)
                        natural_width = max(
                            natural_width,
                            label.fontMetrics().horizontalAdvance(label.text()),
                        )
                    else:
                        label.setObjectName("settingCaptionSpacer")
                    label.installEventFilter(self)
                    labels.append((label, field))

            if not labels:
                return
            viewport = self._settings_scroll_viewport
            content_width = min(
                width for width in (viewport.width(), self.settings_content.width()) if width > 0
            ) if viewport.width() > 0 and self.settings_content.width() > 0 else max(
                viewport.width(), self.settings_content.width(), 700
            )
            usable_width = max(0, content_width - 72)
            max_column_width = max(88, min(320, int(usable_width * 0.38)))
            column_width = min(max_column_width, max(min(112, max_column_width), natural_width))
            stacked = content_width < 640
            if stacked:
                column_width = min(usable_width, natural_width)
            for form in active_forms:
                form.setRowWrapPolicy(
                    QtWidgets.QFormLayout.RowWrapPolicy.WrapAllRows if stacked
                    else QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows
                )
            for label, _field in labels:
                label.setFixedWidth(column_width)
                label.setProperty("settingCaptionColumnWidth", column_width)

            for _ in range(2):
                for card in cards:
                    if card.layout() is not None:
                        card.layout().activate()
                for form in active_forms:
                    form.activate()
                self.settings_layout.activate()
                for label, field in labels:
                    self._align_settings_caption(label, field)

            active_form_ids = {id(form) for form in active_forms}
            for form in self.findChildren(QtWidgets.QFormLayout):
                if id(form) in active_form_ids:
                    continue
                pairs = []
                for row_index in range(form.rowCount()):
                    label_item = form.itemAt(
                        row_index, QtWidgets.QFormLayout.ItemRole.LabelRole
                    )
                    field_item = form.itemAt(
                        row_index, QtWidgets.QFormLayout.ItemRole.FieldRole
                    )
                    label = label_item.widget() if label_item is not None else None
                    field = field_item.widget() if field_item is not None else None
                    if (
                        isinstance(label, QtWidgets.QLabel)
                        and label.objectName() == "settingCaption"
                        and field is not None
                    ):
                        pairs.append((label, field))
                if pairs:
                    form.setLabelAlignment(
                        QtCore.Qt.AlignmentFlag.AlignLeft
                        | QtCore.Qt.AlignmentFlag.AlignTop
                    )
                    form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
                    for label, field in pairs:
                        self._align_settings_caption(label, field)
            self._align_browser_cache_action(
                bool(getattr(self, "_settings_header_compact", True))
            )

        def _align_browser_cache_action(self, compact):
            note = getattr(self, "_browser_cache_status", None)
            button = getattr(self, "_browser_cache_clear_button", None)
            if note is None or button is None:
                return
            from shiboken6 import isValid
            if not isValid(note) or not isValid(button):
                self._browser_cache_status = None
                self._browser_cache_clear_button = None
                return
            default_alignment = getattr(note, "_browser_cache_default_alignment", None)
            if default_alignment is None:
                default_alignment = note.alignment()
            default_margins = getattr(note, "_browser_cache_default_margins", None)
            if default_margins is None:
                default_margins = note.contentsMargins()
            if compact:
                alignment = default_alignment
                top_margin = default_margins.top()
            else:
                alignment = (
                    QtCore.Qt.AlignmentFlag.AlignLeft
                    | QtCore.Qt.AlignmentFlag.AlignTop
                )
                button_height = button.height() or button.sizeHint().height()
                top_margin = max(
                    default_margins.top(),
                    default_margins.top()
                    + (button_height - note.fontMetrics().height()) // 2,
                )
            if note.alignment() != alignment:
                note.setAlignment(alignment)
            margins = note.contentsMargins()
            if margins.top() != top_margin:
                note.setContentsMargins(
                    margins.left(), top_margin, margins.right(), margins.bottom()
                )

        def _add_signing_key_settings(self):
            if credential_recovery_state_loader is not None and credential_recovery_callback is not None:
                if credential_recovery_state_loader().get("pending"):
                    recover = QtWidgets.QPushButton(text["credential_recovery_title"], self)
                    recover.setObjectName("credentialRecoveryButton")
                    recover.clicked.connect(self._offer_credential_recovery)
                    self.settings_layout.addWidget(recover)
            signing = QtWidgets.QFrame(self.settings_content)
            signing.setObjectName("settingsSection")
            signing_layout = QtWidgets.QVBoxLayout(signing)
            description = QtWidgets.QLabel(text["signing_key_description"], signing)
            description.setWordWrap(True)
            signing_layout.addWidget(description)

            actions = QtWidgets.QHBoxLayout()
            button = QtWidgets.QPushButton(text["generate_signing_key"], signing)
            button.setObjectName("signingKeyAction")
            button.clicked.connect(self._generate_signing_key)
            self.signing_key_button = button
            self._settings_search_targets["signingKeyAction"] = button
            actions.addWidget(button)

            status = QtWidgets.QLabel(self._signing_key_feedback, signing)
            status.setObjectName("settingNote")
            status.setWordWrap(True)
            self.signing_key_status = status
            actions.addWidget(status, 1)
            signing_layout.addLayout(actions)
            self.settings_layout.addWidget(signing)
            self._refresh_signing_key_controls()

        def _add_personalization_settings(self):
            card = QtWidgets.QFrame(self.settings_content)
            card.setObjectName("settingsSection")
            card.setProperty("compactSectionBoundary", True)
            form = QtWidgets.QFormLayout(card)
            form.setVerticalSpacing(20)
            title = QtWidgets.QLabel(text.get("personalization", "Personalization"), card)
            title.setObjectName("sectionTitle")
            form.addRow(title)
            theme = QtWidgets.QWidget(card)
            theme.setObjectName("appearanceTheme")
            themes = QtWidgets.QHBoxLayout(theme)
            themes.setContentsMargins(0, 0, 0, 0)
            group = QtWidgets.QButtonGroup(theme)
            group.setExclusive(True)
            themes.addStretch()
            theme_target = None
            for dark, name, key in ((False, "appearanceLight", "theme_light_choice"), (True, "appearanceDark", "theme_dark_choice")):
                button = QtWidgets.QPushButton(text[key], theme)
                button.setObjectName(name)
                button.setCheckable(True)
                button.setChecked(self._dark == dark)
                group.addButton(button)
                button.clicked.connect(lambda _checked=False, value=dark: self._apply_theme(value))
                themes.addWidget(button)
                if theme_target is None:
                    theme_target = button
            self._settings_search_targets["appearanceTheme"] = theme_target
            form.addRow(text.get("appearance_theme", "Theme"), theme)
            def add_row_divider():
                divider = QtWidgets.QFrame(card)
                divider.setObjectName("settingsRowDivider")
                divider.setFixedHeight(1)
                divider.setStyleSheet(
                    "background-color: rgba(128, 140, 160, 72); border: 0;"
                )
                form.addRow(divider)
            add_row_divider()
            font = QtWidgets.QWidget(card)
            font.setObjectName("appearanceFont")
            sizes = QtWidgets.QHBoxLayout(font)
            sizes.setContentsMargins(0, 0, 0, 0)
            sizes.addStretch()
            font_targets = []
            for name, label, delta, enabled in (("appearanceSmaller", "A−", -1, self._font_size > 11), ("appearanceReset", text["appearance_reset"], None, self._font_size != 16), ("appearanceLarger", "A+", 1, self._font_size < 18)):
                button = QtWidgets.QPushButton(label, font)
                button.setObjectName(name)
                button.setEnabled(enabled)
                button.clicked.connect(lambda _checked=False, change=delta: self._change_font_size(16 - self._font_size if change is None else change))
                sizes.addWidget(button)
                if button.isEnabled():
                    font_targets.append(button)
            if font_targets:
                self._settings_search_targets["appearanceFont"] = font_targets[0]
            form.addRow(text.get("appearance_font", "Text size"), font)
            add_row_divider()
            console_row = QtWidgets.QWidget(card)
            console_layout = QtWidgets.QHBoxLayout(console_row)
            console_layout.setContentsMargins(0, 8, 0, 8)
            console_layout.setSpacing(16)
            console_label = QtWidgets.QLabel(text.get("enable_cli_console", "Enable CLI console"), console_row)
            console_label.setObjectName("consoleSettingCaption")
            console_label.setWordWrap(True)
            console_layout.addWidget(console_label, 1)
            console = QtWidgets.QCheckBox(console_row)
            console.setObjectName("enableCliConsole")
            console.setAccessibleName(text.get("enable_cli_console", "Enable CLI console"))
            console.setChecked(self._console_enabled)
            console.setEnabled(self._commands_process is None)
            console.toggled.connect(self._set_console_enabled)
            console_layout.addWidget(console)
            self._settings_search_targets["enableCliConsole"] = console
            form.addRow(console_row)
            self.settings_layout.addWidget(card)

        def _add_search_settings(self):
            from core.gui.settings.services import SEARCH_PRESETS
            from .atlas_icons import icon
            card = QtWidgets.QFrame(self.settings_content)
            card.setObjectName("settingsSection")
            form = QtWidgets.QFormLayout(card)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            selector = QtWidgets.QComboBox(card)
            selector.setObjectName("guidedSearchProvider")
            self._settings_search_targets["guidedSearchProvider"] = selector
            self._style_combo_popup(selector)
            for label, key, url in SEARCH_PRESETS:
                selector.addItem(icon(QtCore, QtGui, key), label, url)
            selector.addItem(text["search_custom"], "custom")
            url_name = "CITATION_VERIFIER_GUIDED_SEARCH_URL"
            query_name = "CITATION_VERIFIER_GUIDED_SEARCH_QUERY"
            current_url = self._settings_dirty.get(url_name, self._settings_initial.get(url_name, SEARCH_PRESETS[0][2]))
            current_query = self._settings_dirty.get(query_name, self._settings_initial.get(query_name, "{raw_entry}"))
            found = selector.findData(current_url)
            selector.setCurrentIndex(found if found >= 0 and current_query == "{raw_entry}" else selector.findData("custom"))
            draft = {"url": current_url, "query": current_query} if selector.currentData() == "custom" else {"url": "", "query": "{raw_entry}"}
            form.addRow(text["search_engine"], selector)
            url = QtWidgets.QLineEdit(current_url, card)
            url.setObjectName("guidedSearchUrl")
            url.setPlaceholderText("https://example.com/search?q={query}")
            query = QtWidgets.QLineEdit(current_query, card)
            query.setObjectName("guidedSearchQuery")
            query.setPlaceholderText("{raw_entry}")
            url_label_text = text["setting_CITATION_VERIFIER_GUIDED_SEARCH_URL_label"]
            query_label_text = text["setting_CITATION_VERIFIER_GUIDED_SEARCH_QUERY_label"]
            url_label = QtWidgets.QLabel(url_label_text, card)
            url_label.setObjectName("settingCaption")
            url_label.setWordWrap(True)
            url_label.installEventFilter(self)
            query_label = QtWidgets.QLabel(query_label_text, card)
            query_label.setObjectName("settingCaption")
            query_label.setWordWrap(True)
            query_label.installEventFilter(self)
            def help_row(field, setting_name, label_text, help_text):
                row = QtWidgets.QWidget(card)
                row_layout = QtWidgets.QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(8)
                row_layout.addWidget(field, 1)
                icon = QtWidgets.QLabel("?", row)
                icon.setObjectName("settingHelpIcon")
                icon.setProperty("settingName", setting_name)
                icon.setFixedSize(22, 22)
                icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                icon.setToolTip(f"<qt>{escape(help_text)}</qt>")
                icon.setAccessibleName(
                    text.get("setting_help_for", "Help for {setting}").format(setting=label_text)
                )
                icon.setAccessibleDescription(help_text)
                row_layout.addWidget(icon, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
                return row
            url_help = text["setting_CITATION_VERIFIER_GUIDED_SEARCH_URL_help"].replace(
                "https://www.google.com/search?q={query}", "https://example.com/search?q={query}"
            )
            query_help = text["setting_CITATION_VERIFIER_GUIDED_SEARCH_QUERY_help"]
            url_field = help_row(url, url_name, url_label_text, url_help)
            query_field = help_row(query, query_name, query_label_text, query_help)
            form.addRow(url_label, url_field)
            form.addRow(query_label, query_field)
            is_custom = selector.currentData() == "custom"
            if is_custom:
                self._settings_search_targets[url_name] = url
                self._settings_search_targets[query_name] = query
            for widget in (url_label, url_field, query_label, query_field):
                widget.setVisible(is_custom)
            def edit_url(value):
                draft["url"] = value
                self._stage_setting(url_name, value)
            def edit_query(value):
                draft["query"] = value
                self._stage_setting(query_name, value)
            url.textEdited.connect(edit_url)
            query.textEdited.connect(edit_query)
            def choose(_index):
                is_custom = selector.currentData() == "custom"
                for widget in (url_label, url_field, query_label, query_field):
                    widget.setVisible(is_custom)
                if is_custom:
                    url.setText(draft["url"])
                    query.setText(draft["query"])
                    self._settings_search_targets[url_name] = url
                    self._settings_search_targets[query_name] = query
                else:
                    url.setText(str(selector.currentData()))
                    query.setText("{raw_entry}")
                    self._settings_search_targets.pop(url_name, None)
                    self._settings_search_targets.pop(query_name, None)
                self._stage_setting(url_name, url.text())
                self._stage_setting(query_name, query.text())
                self._build_settings_search_index()
                self._refresh_settings_search_results()
            selector.currentIndexChanged.connect(choose)
            self.settings_layout.addWidget(card)

        def _add_settings_transfer_controls(self):
            card = QtWidgets.QFrame(self.settings_content)
            card.setObjectName("settingsSection")
            card.setProperty("compactSectionBoundary", True)
            layout = QtWidgets.QVBoxLayout(card)
            title = QtWidgets.QLabel(text["settings_backup_title"], card)
            title.setObjectName("sectionTitle")
            layout.addWidget(title)
            note = QtWidgets.QLabel(text["settings_backup_note"], card)
            note.setWordWrap(True)
            layout.addWidget(note)
            row = QtWidgets.QHBoxLayout()
            self._settings_transfer_row = row
            for action in ("export", "import", "reset"):
                if action == "reset":
                    row.addSpacing(12)
                    self._settings_transfer_reset_gap_index = row.count() - 1
                button = QtWidgets.QPushButton(text["settings_" + action], card)
                button.setObjectName("dangerButton" if action == "reset" else "settingsTransfer" + action.title())
                button.setEnabled(settings_transfer_callback is not None)
                button.clicked.connect(lambda _checked=False, value=action: self._transfer_settings(value))
                row.addWidget(button)
            row.addStretch(1)
            layout.addLayout(row)
            self.settings_layout.addWidget(card)

        def _transfer_settings(self, action):
            if settings_transfer_callback is None or self._loading_kinds.intersection({"settings_transfer", "settings_inspect"}):
                return
            if self._process is not None or self._commands_process is not None or any(
                kind.startswith(("provider_check:", "source_service_check:", "jury_readiness", "llm_preflight"))
                for kind in self._loading_kinds
            ):
                QtWidgets.QMessageBox.information(self, text["tab_settings"], text["settings_transfer_busy"])
                return
            from core.gui.settings.transfer import options
            if action == "import":
                path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, text["settings_import"], "", "JSON (*.json)")
                if path:
                    self._settings_import_path = path
                    self._queue_settings_operation("inspect", {"path": path})
                return
            payload = options(self, QtWidgets, text, action)
            if payload is None:
                return
            if action == "export":
                path, _filter = QtWidgets.QFileDialog.getSaveFileName(self, text["settings_export"], "callimachus-settings.json", "JSON (*.json)")
                if not path:
                    return
                payload["path"] = path
            if action == "export":
                if not self._save_settings():
                    return
                from core.gui.settings.preferences import default_preferences, validate_preferences
                prefs = QtCore.QSettings("Callimachus", "Callimachus")
                try:
                    payload["preferences"] = validate_preferences({
                        key: prefs.value(key, default) for key, default in default_preferences().items()
                    })
                except (TypeError, ValueError):
                    QtWidgets.QMessageBox.warning(self, text["tab_settings"], text["settings_transfer_failed"])
                    return
            self._queue_settings_operation(action, payload)

        def _queue_settings_operation(self, action, payload):
            self.centralWidget().setEnabled(False)
            self._invalidate_load("settings")
            self.statusBar().showMessage(text["settings_transfer_working"])
            self._queue_load("settings_inspect" if action == "inspect" else "settings_transfer", lambda: settings_transfer_callback(action, payload))

        def _apply_transferred_preferences(self, values):
            from core.gui.settings.preferences import validate_preferences
            values = validate_preferences(values)
            prefs = QtCore.QSettings("Callimachus", "Callimachus")
            prefs.clear()
            for key, value in values.items():
                prefs.setValue(key, value)
            prefs.sync()
            self._font_size = int(values["font_size"])
            self._selected_language = values["language"]
            self._set_console_enabled(values["cli_console_enabled"])
            self._apply_theme(values["dark_theme"])
            self._llm_catalogs.clear()
            self._provider_check_state.clear()
            self._service_check_state.clear()
            self._llm_role_preferences_loaded = False
            self._llm_role_preferences_invalid = False
            self.llm_role_choices.clear()
            self._refresh_llm_choices()
            self._refresh_llm_status()
            self.statusBar().showMessage(text["language_restart"], 12000)

        def _set_console_enabled(self, enabled):
            self._console_enabled = bool(enabled)
            QtCore.QSettings("Callimachus", "Callimachus").setValue("cli_console_enabled", self._console_enabled)
            self._console_nav_button.setVisible(self._console_enabled)
            if not self._console_enabled and self._page_index == 4:
                self.navigation.setCurrentRow(3)

        def _update_settings_footer_labels(self):
            footer = getattr(self, "_settings_footer", None)
            layout = footer.layout() if footer is not None else None
            if layout is None:
                return
            margins = layout.contentsMargins()
            buttons = getattr(self, "_settings_footer_buttons", ())
            available_width = (
                footer.width() - margins.left() - margins.right()
                - max(0, layout.spacing()) * len(buttons)
            )
            button_width = max(0, available_width // 2)
            if not buttons or button_width <= 0:
                return
            full_widths = []
            longest_word_widths = []
            for button, label in buttons:
                if button.text() != label:
                    button.setText(label)
                natural_width = button.sizeHint().width()
                full_widths.append(natural_width)
                metrics = button.fontMetrics()
                text_width = metrics.horizontalAdvance(label)
                chrome_width = max(0, natural_width - text_width)
                longest_word_widths.append(
                    max(
                        (metrics.horizontalAdvance(word) for word in label.split()),
                        default=0,
                    ) + chrome_width
                )
            page = getattr(self, "_settings_page", None)
            content_width = footer.width() - margins.left() - margins.right()
            compact = (
                page is None or page.width() < 640 or page.height() < 480
                or sum(full_widths) + max(0, layout.spacing()) * max(0, len(buttons) - 1)
                > content_width
            )
            stacked = compact and button_width < max(longest_word_widths, default=0)
            wrap_width = content_width if stacked else button_width
            wrap = compact and any(width > wrap_width for width in full_widths)
            layout.setDirection(
                QtWidgets.QBoxLayout.Direction.TopToBottom
                if stacked else QtWidgets.QBoxLayout.Direction.LeftToRight
            )
            layout.setStretch(0, 1 if stacked else 0 if compact else 1)
            leading = layout.itemAt(0).spacerItem()
            leading.changeSize(
                0, 0,
                QtWidgets.QSizePolicy.Policy.Expanding
                if not compact and not stacked
                else QtWidgets.QSizePolicy.Policy.Minimum,
                QtWidgets.QSizePolicy.Policy.Expanding
                if stacked else QtWidgets.QSizePolicy.Policy.Minimum,
            )
            for index, (button, label) in enumerate(buttons, 1):
                layout.setStretch(index, 0 if stacked else 1 if compact else 0)
                horizontal_policy = (
                    QtWidgets.QSizePolicy.Policy.Ignored
                    if compact else QtWidgets.QSizePolicy.Policy.Preferred
                )
                policy = button.sizePolicy()
                if policy.horizontalPolicy() != horizontal_policy:
                    button.setSizePolicy(
                        horizontal_policy, QtWidgets.QSizePolicy.Policy.Fixed
                    )
                display = label.replace(" ", "\n", 1) if wrap else label
                if button.text() != display:
                    button.setText(display)
            layout.invalidate()
            footer.updateGeometry()

        def _browser_cache_policy(self):
            policy = str(QtCore.QSettings("Callimachus", "Callimachus").value("browser_cache_policy", "never"))
            return policy if policy in {"never", "fetch", "close"} else "never"

        def _add_browser_cache_settings(self):
            from .atlas_icons import icon
            card = QtWidgets.QFrame(self.settings_content)
            card.setObjectName("settingsSection")
            form = QtWidgets.QFormLayout(card)
            choice = QtWidgets.QComboBox(card)
            choice.setObjectName("browserCachePolicy")
            self._settings_search_targets["browserCachePolicy"] = choice
            self._style_combo_popup(choice)
            for key in ("never", "fetch", "close"):
                choice.addItem(text["browser_cache_" + key], key)
            choice.setCurrentIndex(choice.findData(self._browser_cache_policy()))
            choice.currentIndexChanged.connect(lambda _index: QtCore.QSettings("Callimachus", "Callimachus").setValue("browser_cache_policy", choice.currentData()))
            form.addRow(text["browser_cache_cleanup"], choice)
            note = QtWidgets.QLabel(text["browser_cache_help"], card)
            note.setWordWrap(True)
            note.setMinimumWidth(0)
            note._browser_cache_default_alignment = note.alignment()
            note._browser_cache_default_margins = note.contentsMargins()
            clear = QtWidgets.QPushButton(
                text.get("browser_cache_clear_short", text["browser_cache_clear"]), card
            )
            clear.setObjectName("clearBrowserCache")
            self._settings_search_targets["clearBrowserCache"] = clear
            clear.setProperty("atlasIcon", "trash")
            clear.setProperty("atlasIconSize", 16)
            clear.setIcon(icon(
                QtCore, QtGui, "trash",
                "#ef8997" if self._dark else "#be3046", 16,
            ))
            clear.setIconSize(QtCore.QSize(16, 16))
            clear.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            clear.setToolTip(text["browser_cache_clear"])
            clear.setAccessibleName(text["browser_cache_clear"])
            clear.setAccessibleDescription(text["browser_cache_help"])
            clear.setEnabled(not self._browser_cache_clearing)
            clear.clicked.connect(lambda: self._clear_browser_cache())
            self._browser_cache_clear_button = clear
            action_row = QtWidgets.QWidget(card)
            action_layout = QtWidgets.QHBoxLayout(action_row)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(8)
            action_layout.addWidget(note, 1)
            action_layout.addWidget(
                clear, 0,
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop,
            )
            form.addRow("", action_row)
            self._browser_cache_status = note
            self.settings_layout.addWidget(card)

        def _clear_browser_cache(self):
            if self._browser_cache_clearing:
                return
            self._browser_cache_clearing = True
            worker = getattr(self._guided_window, "_browser_worker", None)
            if worker is not None and hasattr(worker, "cache_cleared"):
                worker.cache_cleared.connect(self._browser_live_cache_cleared, QtCore.Qt.ConnectionType.SingleShotConnection)
                QtCore.QMetaObject.invokeMethod(worker, "clear_cache", QtCore.Qt.ConnectionType.QueuedConnection)
                return
            self._clear_offline_browser_cache()

        @QtCore.Slot(dict)
        def _browser_live_cache_cleared(self, result):
            profile = str(self._institutional_config().get("profile") or "guided_fetch")
            self._clear_offline_browser_cache((profile,), live_ok=bool(result.get("ok")))

        def _clear_offline_browser_cache(self, exclude=(), *, live_ok=True):
            from core.app.browser_cache import clear_browser_caches
            if self._load_pool is None:
                self._load_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="callimachus-cache")
            def clear():
                result = clear_browser_caches(exclude_profiles=exclude)
                return {**result, "ok": bool(result.get("ok")) and live_ok}
            self._queue_load("browser_cache", clear)

        @staticmethod
        def _clear_layout(layout):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().hide()
                    item.widget().deleteLater()
                elif item.layout() is not None:
                    DesktopWindow._clear_layout(item.layout())
                    item.layout().deleteLater()
            layout.deleteLater()

        def _add_setting_row(self, form, name: str, label_text: str, help_text: str, field, note):
            form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setLabelAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
            )
            row = QtWidgets.QWidget(field.parentWidget())
            layout = QtWidgets.QGridLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setHorizontalSpacing(8)
            layout.setColumnStretch(0, 1)
            label = QtWidgets.QLabel(label_text)
            label.setObjectName("settingCaption")
            label.setProperty("settingName", name)
            label.setWordWrap(True)
            label.installEventFilter(self)
            layout.addWidget(field, 0, 0)
            icon = QtWidgets.QLabel("?", row)
            icon.setObjectName("settingHelpIcon")
            icon.setProperty("settingName", name)
            icon.setFixedSize(22, 22)
            icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            icon.setToolTip(f"<qt>{escape(help_text)}</qt>")
            icon.setAccessibleName(
                text.get("setting_help_for", "Help for {setting}").format(setting=label_text)
            )
            icon.setAccessibleDescription(help_text)
            layout.addWidget(icon, 0, 1, QtCore.Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(note, 1, 0)
            form.addRow(label, row)
            self._align_settings_caption(label, field)

        def _add_setting_editor(self, form, parent, row: Mapping[str, Any]):
            name = str(row.get("name") or "")
            if not name:
                return
            label_text = str(
                text.get(f"setting_{name}_label")
                or row.get("label")
                or text["setting_custom_label"]
            )
            summary = str(
                text.get(f"setting_{name}_summary")
                or text["setting_unknown_summary"]
            )
            help_text = str(
                text.get(f"setting_{name}_help")
                or text["setting_unknown_help"]
            )
            field = QtWidgets.QWidget(parent); layout = QtWidgets.QVBoxLayout(field); layout.setContentsMargins(0, 0, 0, 0)
            control = str(row.get("control") or "text")
            secret = control == "secret"
            configured = bool(row.get("configured"))
            value = self._settings_dirty.get(name, str(row.get("value") or ""))
            model_provider = str(row.get("model_provider") or "")
            if model_provider:
                editor = QtWidgets.QComboBox(field)
                editor.setEditable(True)
                editor.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
                self._style_combo_popup(editor)
                model_values = list(
                    (self._llm_catalogs.get(model_provider) or {}).get("models") or ()
                )
                for choice in row.get("choices") or ():
                    if isinstance(choice, Mapping):
                        choice_value = str(choice.get("value") or "")
                    else:
                        choice_value = str(choice)
                    if choice_value and choice_value not in model_values:
                        model_values.append(choice_value)
                editor.addItems(model_values)
                if value and editor.findText(value) < 0:
                    editor.addItem(value)
                index = editor.findText(value) if value else -1
                editor.setCurrentIndex(index)
                if index < 0:
                    editor.setEditText(value)
                self._provider_model_editors.setdefault(model_provider, []).append(editor)
                editor.currentTextChanged.connect(
                    lambda current, n=name: self._stage_setting(n, current)
                )
            elif name == "OPENAI_BASE_URL":
                editor = QtWidgets.QComboBox(field)
                editor.setEditable(True)
                editor.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
                presets = (
                    ("OpenAI", "https://api.openai.com/v1"),
                )
                for provider_name, endpoint in presets:
                    editor.addItem(endpoint)
                    editor.setItemData(
                        editor.count() - 1, provider_name,
                        QtCore.Qt.ItemDataRole.ToolTipRole,
                    )
                self._style_combo_popup(editor)
                endpoint_value = value or "https://api.openai.com/v1"
                index = editor.findText(endpoint_value, QtCore.Qt.MatchFlag.MatchExactly)
                if index >= 0:
                    editor.setCurrentIndex(index)
                else:
                    editor.setCurrentIndex(-1)
                    editor.setEditText(endpoint_value)
                editor.editTextChanged.connect(
                    lambda current, n=name: self._stage_setting(n, current)
                )
            elif control in {"boolean", "enum"}:
                editor = QtWidgets.QComboBox(field)
                self._style_combo_popup(editor)
                choices = row.get("choices") or []
                for choice in choices:
                    if isinstance(choice, Mapping): editor.addItem(str(choice.get("label") or choice.get("value") or ""), str(choice.get("value") or ""))
                found = editor.findData(value)
                if found >= 0: editor.setCurrentIndex(found)
                editor.currentIndexChanged.connect(lambda _i, n=name, e=editor: self._stage_setting(n, str(e.currentData())))
            elif control == "integer":
                editor = QtWidgets.QSpinBox(field); editor.setRange(0, 999999)
                if not configured:
                    editor.setSpecialValueText(
                        text["setting_empty"] if row.get("explicitly_empty")
                        else text["setting_unconfigured"]
                    )
                try: editor.setValue(int(value) if value else editor.minimum())
                except (TypeError, ValueError): pass
                editor.valueChanged.connect(
                    lambda number, n=name: self._stage_setting(n, str(number))
                )
            else:
                editor = QtWidgets.QLineEdit(field)
                if secret:
                    editor.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
                    editor.setPlaceholderText(text["secret_configured"] if configured else text["secret_empty"])
                    replacing = name in self._settings_replacing
                    editor.setText("••••••••" if configured and not replacing else value)
                    editor.setReadOnly(configured and not replacing)
                    editor.setProperty("secretName", name)
                    editor.setProperty("secretConfigured", configured)
                    editor.installEventFilter(self)
                    if configured:
                        editor.editingFinished.connect(lambda n=name, e=editor: self._restore_empty_secret(n, e))
                    layout.addWidget(editor)
                    self._secret_editors[name] = editor
                else:
                    editor.setText(value); layout.addWidget(editor)
                if secret:
                    editor.textEdited.connect(lambda value, n=name: self._stage_secret(n, value))
                else:
                    editor.textEdited.connect(lambda value, n=name: self._stage_setting(n, value))
            if not secret or not configured:
                if layout.count() == 0: layout.addWidget(editor)
            editor.setProperty("unconfigured", not configured and not bool(row.get("explicitly_empty")))
            status = self._setting_status(row)
            note = QtWidgets.QLabel(" · ".join(item for item in (summary, status) if item), field)
            note.setObjectName("settingNote")
            note.setProperty("settingName", name)
            note.setWordWrap(True)
            self._settings_editors[name] = editor
            self._settings_search_targets[name] = editor
            if secret:
                self._attach_api_key_action(name, editor, str(row.get("check_provider") or ""))
            self._add_setting_row(form, name, label_text, help_text, field, note)

        def _attach_api_key_action(self, name, editor, provider):
            import weakref
            from core.gui.settings.services import KEY_PORTALS
            if name not in KEY_PORTALS:
                return
            action = QtWidgets.QPushButton(editor)
            action.setObjectName("apiKeyAction")
            action.setFixedHeight(28)
            action.setMinimumWidth(76)
            action.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            layout = QtWidgets.QHBoxLayout(editor)
            layout.setContentsMargins(0, 0, 5, 0)
            layout.addStretch()
            layout.addWidget(action, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
            editor.setTextMargins(0, 0, 88, 0)
            self._api_key_actions[name] = (weakref.ref(action), weakref.ref(editor), provider)

            def changed(_value):
                self._service_check_state.pop(name, None)
                self._invalidate_load("source_service_check:" + name)
                self._refresh_api_key_actions()
            editor.textEdited.connect(changed)

            def clicked():
                if not editor.text().strip() and not editor.property("secretConfigured"):
                    QtGui.QDesktopServices.openUrl(QtCore.QUrl(KEY_PORTALS[name]))
                elif provider:
                    self._start_provider_check(provider)
                    self._refresh_api_key_actions()
                elif source_service_check_callback is not None:
                    self._start_source_service_check(name)
            action.clicked.connect(clicked)
            self._refresh_api_key_actions()

        def _fail_source_service_check(self, name: str, message: str):
            self._service_check_state[name] = {"ok": False, "error": message}
            self._refresh_api_key_actions()
            self._show_provider_check_warning(message)

        def _expire_source_service_check(self, name: str, kind: str, token: int):
            current = self._source_service_check_watchdogs.get(kind)
            if current is None or current[0] != token:
                return
            if self._load_closed or self._load_tokens.get(kind) != token:
                self._stop_source_service_check_watchdog(kind, token)
                return
            self._invalidate_load(kind)
            self._fail_source_service_check(
                name, text["source_service_check_timeout"],
            )

        def _start_source_service_check(self, name: str):
            kind = "source_service_check:" + name
            if (
                self._load_closed or self._load_pool is None
                or kind in self._loading_kinds
            ):
                return
            self._service_check_state[name] = {"checking": True}
            self._refresh_api_key_actions()
            try:
                self._queue_load(
                    kind,
                    lambda delta=dict(self._settings_dirty):
                        source_service_check_callback(name, delta),
                )
            except Exception:
                self._invalidate_load(kind)
                self._fail_source_service_check(name, text["provider_check_error"])
                return
            token = self._load_tokens.get(kind, 0)
            if kind not in self._loading_kinds or kind not in self._load_futures:
                self._fail_source_service_check(name, text["provider_check_error"])
                return
            watchdog = QtCore.QTimer(self)
            watchdog.setSingleShot(True)
            watchdog.timeout.connect(
                lambda n=name, k=kind, t=token:
                    self._expire_source_service_check(n, k, t)
            )
            self._source_service_check_watchdogs[kind] = (token, watchdog)
            watchdog.start(max(1, int(_SOURCE_SERVICE_CHECK_TIMEOUT_MS)))

        def _refresh_api_key_actions(self):
            from core.gui.settings.services import KEY_PORTALS
            for name, (button_ref, editor_ref, provider) in list(self._api_key_actions.items()):
                button, editor = button_ref(), editor_ref()
                if button is None or editor is None:
                    self._api_key_actions.pop(name, None)
                    continue
                try:
                    filled = bool(editor.text().strip()) or bool(editor.property("secretConfigured"))
                    state = (self._provider_check_state.get(provider) or {}) if provider else (self._service_check_state.get(name) or {})
                    checking = state.get("checking") or state.get("status") == "checking"
                    can_check = provider_check_callback is not None if provider else source_service_check_callback is not None
                    button.setEnabled(not checking and (not filled or can_check))
                    status = "success" if state.get("ok") or state.get("status") == "success" else "error" if state.get("ok") is False or state.get("status") == "error" else ""
                    button.setText("Check…" if checking and filled else text["api_key_verified"] if filled and status == "success" else "Check" if filled else "Get one")
                    editor.setTextMargins(0, 0, max(76, button.sizeHint().width()) + 12, 0)
                    button.setProperty("checkStatus", status if filled else "")
                    button.setToolTip(KEY_PORTALS[name] if not filled else text["api_key_recheck"] if status == "success" else text["provider_check_button"])
                    button.style().unpolish(button)
                    button.style().polish(button)
                except RuntimeError:
                    self._api_key_actions.pop(name, None)

        def _setting_status(self, row: Mapping[str, Any]) -> str:
            if row.get("configured"):
                state = text["setting_configured"]
            elif row.get("explicitly_empty"):
                state = text["setting_empty"]
            else:
                state = text["setting_unconfigured"]
            source = str(row.get("source") or "")
            source = text.get("setting_source_" + source, "")
            return f"{state}{' · ' + source if source else ''}"

        def _begin_secret_replacement(self, name, editor):
            if name not in self._settings_replacing:
                self._settings_replacing.add(name)
                editor.setReadOnly(False)
                editor.clear()
                self._invalidate_provider_catalog_for_setting(name)
            editor.setFocus()

        def _stage_secret(self, name: str, value: str):
            self._settings_revision += 1
            # An empty field means "do not replace"; the existing secret remains intact.
            self._invalidate_provider_catalog_for_setting(name)
            if value:
                self._settings_dirty[name] = value
            else:
                self._settings_dirty.pop(name, None)

        def _restore_empty_secret(self, name, editor):
            if name in self._settings_replacing and not editor.text() and name not in self._settings_dirty:
                self._settings_replacing.discard(name)
                editor.setReadOnly(True)
                editor.setText("••••••••")

        def _signing_key_action_state(self) -> str:
            if not self._settings_loaded:
                return "unknown"
            key_row = next((
                row for row in self._settings_rows
                if row.get("name") == "CITATION_VERIFIER_SIGNING_KEY"
            ), None)
            if key_row is not None:
                source = str(key_row.get("source") or "")
                configured = bool(key_row.get("configured"))
                if source == "credential_store" and configured:
                    return "configured"
                if source == "environment":
                    return "external_configured" if configured else "external_unconfigured"
                if source == "env_file" and configured:
                    return "external_configured"
                if configured:
                    return "external_configured"

            path_row = next((
                row for row in self._settings_rows
                if row.get("name") == "CITATION_VERIFIER_SIGNING_KEY_FILE"
            ), None)
            if path_row is not None and path_row.get("configured"):
                path = str(path_row.get("value") or "").strip()
                if not path:
                    return "unknown"
                try:
                    if Path(path).exists():
                        return "configured"
                except OSError:
                    return "unknown"
            return "absent"

        def _refresh_signing_key_controls(self):
            button = getattr(self, "signing_key_button", None)
            status_label = getattr(self, "signing_key_status", None)
            state = self._signing_key_action_state()
            try:
                if button is not None:
                    button.setText(
                        text["signing_key_regenerate"]
                        if state in {"configured", "external_configured"}
                        else text["signing_key_action_unknown"]
                        if state == "unknown"
                        else text["generate_signing_key"]
                    )
                    button.setEnabled(state in {"absent", "configured"})
                if status_label is not None:
                    status = self._signing_key_feedback
                    state_message = (
                        text["signing_key_external"]
                        if state in {"external_configured", "external_unconfigured"}
                        else text["signing_key_status_unknown"]
                        if state == "unknown" else ""
                    )
                    status_label.setText("\n".join(
                        part for part in (status, state_message) if part
                    ))
            except RuntimeError:
                # A category rebuild can retire the previous controls before
                # Qt delivers their deferred deletion events.
                return

        def _generate_signing_key(self):
            state = self._signing_key_action_state()
            if state in {"unknown", "external_configured", "external_unconfigured"}:
                self._refresh_signing_key_controls()
                return
            regenerate = state == "configured"
            if regenerate:
                dialog = QtWidgets.QMessageBox(self)
                dialog.setObjectName("signingKeyRegenerationDialog")
                dialog.setIcon(QtWidgets.QMessageBox.Icon.Question)
                QtWidgets.QWidget.setWindowTitle(
                    dialog, text["signing_key_regenerate_title"]
                )
                dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                dialog.setText(text["signing_key_regenerate_confirmation"])
                regenerate_button = dialog.addButton(
                    text["signing_key_regenerate_action"],
                    QtWidgets.QMessageBox.ButtonRole.DestructiveRole,
                )
                regenerate_button.setObjectName("dangerButton")
                cancel = dialog.addButton(
                    text["cancel_action"],
                    QtWidgets.QMessageBox.ButtonRole.RejectRole,
                )
                cancel.setObjectName("signingKeyCancel")
                dialog.setDefaultButton(cancel)
                dialog.setEscapeButton(cancel)
                from .atlas_icons import icon as atlas_icon
                icon_color = "#e6edf6" if self._dark else "#11183c"
                dialog.setIconPixmap(
                    atlas_icon(QtCore, QtGui, "help", color=icon_color, size=36).pixmap(
                        QtCore.QSize(36, 36)
                    )
                )
                self._prepare_confirmation_dialog(dialog)
                dialog.exec()
                if dialog.clickedButton() is not regenerate_button:
                    return
            staged = dict(self._settings_dirty)
            replacing = set(self._settings_replacing)
            try:
                result = (
                    signing_key_generator(regenerate=True)
                    if regenerate else signing_key_generator()
                ) or {}
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, text["tab_settings"], str(exc))
                return
            path = str(result.get("path") or "")
            created = bool(result.get("created"))
            status = (
                text["signing_key_regenerated"] if result.get("regenerated")
                else text["signing_key_created"] if created
                else text["signing_key_exists"]
            )
            storage = str(result.get("storage") or "")
            storage_label = (
                text.get("signing_key_source_" + storage, "")
                if storage in {"keyring", "environment"} else ""
            )
            location = path or storage_label
            message = f"{status}{': ' + location if location else ''}"
            self._signing_key_feedback = message
            # Reload inventory state, retaining the current settings draft.
            self._settings_loaded = False
            self._refresh_signing_key_controls()
            self._refresh_static_tabs()
            self._settings_dirty.update(staged)
            self._settings_replacing.update(replacing)
            general = next(
                (index for index in range(self.settings_categories.count())
                 if self._settings_categories_key(index) == "general"),
                -1,
            )
            if general >= 0:
                self.settings_categories.setCurrentRow(general)
                self._render_settings_category("general")
            self._refresh_signing_key_controls()

            self.statusBar().showMessage(message, 12_000)

        def _stage_setting(self, name: str, value: str):
            self._settings_revision += 1
            self._invalidate_provider_catalog_for_setting(name)
            initial = self._settings_initial.get(name, "")
            if name not in self._settings_initial:
                # Match the defaults displayed by the search controls. Returning
                # to an implicit default must not create an unsaved override.
                if name == "CITATION_VERIFIER_GUIDED_SEARCH_URL":
                    from core.gui.settings.services import SEARCH_PRESETS
                    initial = SEARCH_PRESETS[0][2]
                elif name == "CITATION_VERIFIER_GUIDED_SEARCH_QUERY":
                    initial = "{raw_entry}"
            if value == initial:
                self._settings_dirty.pop(name, None)
            else:
                self._settings_dirty[name] = value

        def _save_settings(self) -> bool:
            if not self._settings_dirty:
                return True
            updates = dict(self._settings_dirty)
            try:
                if "CITATION_VERIFIER_MAILTO" in updates:
                    from core.gui.settings.services import valid_email
                    value = updates["CITATION_VERIFIER_MAILTO"].strip()
                    if value and not valid_email(value):
                        raise ValueError(text["source_contact_invalid"])
                    updates["CITATION_VERIFIER_MAILTO"] = value
                search_names = {"CITATION_VERIFIER_GUIDED_SEARCH_URL", "CITATION_VERIFIER_GUIDED_SEARCH_QUERY"}
                if search_names.intersection(updates):
                    if "CITATION_VERIFIER_GUIDED_SEARCH_URL" in updates and not updates["CITATION_VERIFIER_GUIDED_SEARCH_URL"].strip():
                        raise ValueError(text["search_custom_required"])
                    from .guided_fetch_viewmodel import GuidedFetchViewModel
                    search_values = {name: value for name, value in self._settings_initial.items() if name in search_names}
                    GuidedFetchViewModel({"references": []}, env={**search_values, **updates})
                context_names = {"CITATION_VERIFIER_VERIFY_CONTEXT_MODE",
                                 "CITATION_VERIFIER_CONTEXT_PROFILE",
                                 "CITATION_VERIFIER_MAX_SOURCE_CHARS"}
                if context_names.intersection(updates):
                    from core.verify.claim_evidence.context_config import resolve_context_settings
                    context = {str(row["name"]): str(row.get("value") or "")
                               for row in self._settings_rows if row.get("name") in context_names}
                    resolve_context_settings({**context, **updates})
                if updates: settings_saver(updates)
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, text["tab_settings"], str(exc)); return False
            accuracy = updates.get("CITATION_VERIFIER_ACCURACY")
            if accuracy in self.mode_buttons:
                self.mode_buttons[accuracy].setChecked(True)
            self._settings_dirty.clear(); self._settings_replacing.clear(); self._settings_loaded = False
            self._refresh_llm_choices()
            selected_preview = self._selected_llm_preview()
            overlay = selected_preview.get("overlay")
            overlay_already_saved = (
                bool(selected_preview.get("available"))
                and isinstance(overlay, Mapping)
                and bool(overlay)
                and not any(_is_secret_setting_name(str(name)) for name in overlay)
                and all(
                    self._settings_initial.get(str(name)) == str(value)
                    for name, value in overlay.items()
                )
            )
            overlay_saved = (
                True if self._jury_action_selection_active
                else overlay_already_saved or self._persist_selected_llm_overlay(
                    selected_preview
                )
            )
            self._refresh_llm_status(preview=selected_preview)
            if self._background_loading:
                self._queue_load("settings", settings_loader)
            self._refresh_static_tabs()
            if not overlay_saved:
                QtWidgets.QMessageBox.warning(
                    self,
                    text["llm_role_save_failed_title"],
                    text["llm_role_save_failed"],
                )
                return False
            return True

        def _discard_settings(self):
            self._settings_dirty.clear(); self._settings_replacing.clear(); self._settings_loaded = False
            self._refresh_static_tabs()

        def _confirm_settings_changes(self) -> str:
            prompt = QtWidgets.QMessageBox(self); prompt.setWindowTitle(text["tab_settings"])
            prompt.setText(text["unsaved_settings"])
            save = prompt.addButton(text["save_changes"], QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            save.setObjectName("primaryButton")
            discard = prompt.addButton(text["discard_changes"], QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            discard.setObjectName("dangerButton")
            keep_editing = prompt.addButton(text["keep_editing"], QtWidgets.QMessageBox.ButtonRole.RejectRole)
            keep_editing.setObjectName("settingsKeepEditing")
            prompt.setDefaultButton(keep_editing)
            prompt.setEscapeButton(keep_editing)
            prompt.exec()
            return "save" if prompt.clickedButton() is save else "discard" if prompt.clickedButton() is discard else "cancel"

        def _language_selector(self, parent):
            selector = QtWidgets.QComboBox(parent)
            selector.setObjectName("applicationLanguage")
            selector.addItem("English", "en")
            selector.addItem("Italiano", "it")
            selector.setCurrentIndex(max(0, selector.findData(self._selected_language)))
            self._style_combo_popup(selector)
            selector.currentIndexChanged.connect(lambda _index: self._change_language(str(selector.currentData())))
            selector.setToolTip(text["language_restart"])
            return selector

        def _change_language(self, language):
            preference_saver(language)
            self._selected_language = language
            if language == self._language:
                self._restart_language = None
                return
            answer = QtWidgets.QMessageBox.question(
                self, text["tab_settings"], text["language_restart_question"],
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            if self._settings_dirty:
                decision = self._confirm_settings_changes()
                if decision == "cancel":
                    return
                if decision == "save" and not self._save_settings():
                    return
                if decision == "discard":
                    self._discard_settings()
            self._restart_language = language
            self.close()
            # Busy work and cancelled unsaved-changes prompts must not leave a
            # surprise restart armed for an unrelated later close.
            if self.isVisible() and not any((
                self._browser_cache_close_pending,
                getattr(self, "_close_after_guided", False),
                getattr(self, "_institutional_close_pending", False),
            )):
                self._restart_language = None

        def _render_documentation(self, documents):
            if isinstance(documents, str):
                documents = {"README.md": documents}
            self._documents = {str(path): str(body) for path, body in dict(documents).items()}
            visible_paths = {
                "README.md",
                "privacy-it.md" if self._language == "it" else "privacy.md",
                "Third-party notices.md",
            }
            visible_paths.update(
                path for path in self._documents
                if re.fullmatch(r"\d{2}-[^/\\]+\.md", path)
            )
            self.docs_outline.blockSignals(True)
            self.docs_outline.clear()
            for path, markdown in self._documents.items():
                if path not in visible_paths:
                    continue
                title = next((line.lstrip("#").strip() for line in markdown.splitlines()
                              if line.startswith("# ")), Path(path).stem.replace("-", " "))
                document = QtWidgets.QTreeWidgetItem([title])
                document_font = document.font(0)
                document_font.setBold(True)
                document.setFont(0, document_font)
                document.setToolTip(0, title)
                document.setData(0, QtCore.Qt.ItemDataRole.UserRole, (path, ""))
                document.setExpanded(True)
                self.docs_outline.addTopLevelItem(document)
                for line in markdown.splitlines():
                    if line.startswith("## "):
                        heading = line[3:].strip()
                        child = QtWidgets.QTreeWidgetItem([heading])
                        child.setToolTip(0, heading)
                        child.setData(0, QtCore.Qt.ItemDataRole.UserRole, (path, heading))
                        document.addChild(child)
            self.docs_outline.blockSignals(False)
            if self.docs_outline.topLevelItemCount():
                self.docs_outline.setCurrentItem(self.docs_outline.topLevelItem(0))
            if getattr(self, "_pending_privacy_document", False):
                self._pending_privacy_document = False
                self._select_privacy_document()

        def _show_privacy_policy(self):
            self._atlas_guide.show_privacy_document(self.docs_view)
            self._pending_privacy_document = True
            self.navigation.setCurrentRow(5)
            if self._documents:
                self._pending_privacy_document = False
                self._select_privacy_document()

        def _select_privacy_document(self):
            target = "privacy-it.md" if self._language == "it" else "privacy.md"
            for index in range(self.docs_outline.topLevelItemCount()):
                item = self.docs_outline.topLevelItem(index)
                if item.data(0, QtCore.Qt.ItemDataRole.UserRole)[0] == target:
                    self.docs_outline.setCurrentItem(item)
                    return
            self.docs_view.setPlainText(text["no_documentation"])

        def _report_ai_content(self):
            # Open a blank issue form. Never attach manuscript, logs or credentials.
            url = QtCore.QUrl("https://github.com/SNatangelo/Callimachus/issues/new")
            query = QtCore.QUrlQuery()
            query.addQueryItem("title", "AI content concern")
            url.setQuery(query)
            QtGui.QDesktopServices.openUrl(url)

        def _navigate_documentation(self, item, _previous=None):
            if item is None:
                return
            path, anchor = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ("", "")
            self._display_documentation(path, anchor)

        def _display_documentation(self, path, anchor=""):
            self._current_doc_path = str(path)
            markdown = self._documents.get(str(path), text["no_documentation"])
            self.docs_view.document().setDefaultStyleSheet(_documentation_css(dark=self._dark, font_size=self._font_size))
            self.docs_view.setMarkdown(markdown)
            self._format_documentation_document()
            if anchor:
                cursor = self.docs_view.document().find(str(anchor))
                if not cursor.isNull(): self.docs_view.setTextCursor(cursor); self.docs_view.ensureCursorVisible()

        def _format_documentation_document(self):
            """Apply spacing and code surfaces that Qt's Markdown CSS omits."""
            blocks = []
            block = self.docs_view.document().begin()
            while block.isValid():
                blocks.append(block)
                block = block.next()
            code_background = QtGui.QColor("#111827" if self._dark else "#e7eef8")
            code_foreground = QtGui.QColor("#edf3ff" if self._dark else "#172033")
            code_border = QtGui.QColor("#3b4759" if self._dark else "#b7c6da")
            code_groups = []
            active_group = []
            for block in blocks:
                if block.blockFormat().nonBreakableLines():
                    active_group.append(block)
                elif active_group:
                    code_groups.append(active_group)
                    active_group = []
            if active_group:
                code_groups.append(active_group)

            for block in blocks:
                block_format = block.blockFormat()
                cursor = QtGui.QTextCursor(block)
                if (
                    not block_format.nonBreakableLines()
                    and block.text().strip()
                    and block.textList() is None
                    and block_format.headingLevel() == 0
                ):
                    block_format.setTopMargin(3)
                    block_format.setBottomMargin(13)
                    block_format.setLineHeight(
                        145.0,
                        QtGui.QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
                    )
                    cursor.setBlockFormat(block_format)

            for group in reversed(code_groups):
                code = "\n".join(block.text() for block in group)
                cursor = QtGui.QTextCursor(self.docs_view.document())
                cursor.setPosition(group[0].position())
                cursor.setPosition(
                    group[-1].position() + len(group[-1].text()),
                    QtGui.QTextCursor.MoveMode.KeepAnchor,
                )
                cursor.removeSelectedText()
                table_format = QtGui.QTextTableFormat()
                table_format.setWidth(QtGui.QTextLength(
                    QtGui.QTextLength.Type.PercentageLength, 100
                ))
                table_format.setBorder(1)
                table_format.setBorderStyle(
                    QtGui.QTextFrameFormat.BorderStyle.BorderStyle_Solid
                )
                table_format.setBorderBrush(code_border)
                table_format.setBackground(code_background)
                table_format.setCellPadding(11)
                table_format.setCellSpacing(0)
                table_format.setTopMargin(11)
                table_format.setBottomMargin(11)
                table = cursor.insertTable(1, 1, table_format)
                code_cursor = table.cellAt(0, 0).firstCursorPosition()
                character_format = QtGui.QTextCharFormat()
                character_format.setForeground(code_foreground)
                character_format.setFontFamilies(["Consolas", "monospace"])
                character_format.setFontPointSize(max(11, self._font_size))
                code_cursor.insertText(code, character_format)

            link_color = QtGui.QColor("#a8d1ff" if self._dark else "#174a8b")
            block = self.docs_view.document().begin()
            while block.isValid():
                for iterator in block:
                    fragment = iterator.fragment()
                    if not fragment.charFormat().isAnchor():
                        continue
                    cursor = QtGui.QTextCursor(self.docs_view.document())
                    cursor.setPosition(fragment.position())
                    cursor.setPosition(
                        fragment.position() + fragment.length(),
                        QtGui.QTextCursor.MoveMode.KeepAnchor,
                    )
                    format = fragment.charFormat()
                    format.setForeground(link_color)
                    format.setFontUnderline(True)
                    cursor.setCharFormat(format)
                block = block.next()

            if (
                getattr(self, "_current_doc_path", "") in {"privacy.md", "privacy-it.md"}
                and hasattr(self, "_atlas_guide")
            ):
                self._atlas_guide.format_privacy_document()

        def _open_doc_link(self, url):
            if url.scheme() in {"http", "https", "mailto"}:
                QtGui.QDesktopServices.openUrl(url); return
            raw = url.toString()
            target, _, anchor = raw.partition("#")
            if not target:
                target = getattr(self, "_current_doc_path", "README.md")
            elif target.startswith("/"):
                return
            else:
                current = getattr(self, "_current_doc_path", "README.md")
                reference_root = "docs/reference"
                reference_source = posixpath.normpath(posixpath.join(reference_root, current))
                reference_target = posixpath.relpath(
                    posixpath.normpath(posixpath.join(posixpath.dirname(reference_source), target)),
                    reference_root,
                )
                if reference_target in self._documents:
                    target = reference_target
                else:
                    privacy_language = {
                        "privacy.md": "en",
                        "privacy-it.md": "it",
                    }.get(current)
                    if privacy_language:
                        source = f"docs/legal/{privacy_language}/privacy.md"
                    else:
                        source = f"docs/user/{self._language}/{current}"
                    destination = posixpath.normpath(
                        posixpath.join(posixpath.dirname(source), target)
                    )
                    guide_root = f"docs/user/{self._language}/"
                    privacy_path = f"docs/legal/{self._language}/privacy.md"
                    if destination.startswith(guide_root):
                        target = destination[len(guide_root):]
                    elif destination == privacy_path:
                        target = "privacy-it.md" if self._language == "it" else "privacy.md"
                    else:
                        return
            if target in self._documents:
                for index in range(self.docs_outline.topLevelItemCount()):
                    item = self.docs_outline.topLevelItem(index)
                    if item.data(0, QtCore.Qt.ItemDataRole.UserRole)[0] == target:
                        current_item = self.docs_outline.currentItem()
                        current_data = (
                            current_item.data(0, QtCore.Qt.ItemDataRole.UserRole)
                            if current_item is not None else None
                        )
                        current_path = (current_data or ("", ""))[0]
                        if current_path == target:
                            self._display_documentation(target, anchor.replace("-", " "))
                            return
                        self.docs_outline.setCurrentItem(item)
                        if anchor:
                            cursor = self.docs_view.document().find(anchor.replace("-", " "))
                            if not cursor.isNull(): self.docs_view.setTextCursor(cursor); self.docs_view.ensureCursorVisible()
                        return
            if target in self._documents:
                self.docs_outline.setCurrentItem(None)
                self._display_documentation(target, anchor.replace("-", " "))

        def _render_cache(self):
            query = self.cache_search.text().casefold().strip()
            selected = self.cache_filter.currentText()
            selected_key = {
                text["all"]: "",
                text["fulltext"]: "fulltext",
                text["abstract"]: "abstract",
                text["no_text"]: "none",
            }.get(selected, "")
            rows = [
                row
                for row in getattr(self, "_cache_rows", [])
                if cache_row_matches_query(row, query)
                and (
                    not selected_key
                    or str(row.get("tier") or row.get("availability") or "").lower()
                    == selected_key
                )
            ]
            self._cache_filtered_rows = rows
            if self._cache_visible_count <= 0:
                self._cache_visible_count = self._page_batch_size(self.cache_table)
            pending = self._library_import_rows
            self.cache_more_button.setVisible(
                len(self._cache_rows) < self._atlas_cache_total if cache_page_loader is not None
                else len(pending) + len(rows) > self._cache_visible_count)
            self.cache_table.blockSignals(True)
            self.cache_table.clearSelection()
            self._library_skeleton_effects.clear()
            for index in range(self.cache_table.rowCount()):
                self.cache_table.removeCellWidget(index, 3)
            self.cache_table.setRowCount(min(len(pending) + len(rows), self._cache_visible_count))
            for index, row in enumerate(pending[:self._cache_visible_count]):
                filename = Path(row["file"]).name
                stage = row["stage"]
                for column, value in enumerate(("", filename, text[f"library_activity_{stage}"], "")):
                    item = QtWidgets.QTableWidgetItem(value)
                    item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                    item.setToolTip(value if column == 2 else row["file"])
                    self.cache_table.setItem(index, column, item)
                if stage != "processed":
                    holder = QtWidgets.QWidget(self.cache_table)
                    holder.setObjectName("libraryLoadingCell")
                    holder_layout = QtWidgets.QHBoxLayout(holder)
                    holder_layout.setContentsMargins(8, 0, 8, 0)
                    bar = QtWidgets.QFrame(holder)
                    bar.setObjectName("libraryImportSkeletonBar")
                    bar.setFixedSize(100, 8)
                    holder_layout.addWidget(bar)
                    holder_layout.addStretch()
                    effect = QtWidgets.QGraphicsOpacityEffect(holder)
                    holder.setGraphicsEffect(effect)
                    self._library_skeleton_effects.append(effect)
                    self.cache_table.setCellWidget(index, 3, holder)
            for index, row in enumerate(rows[:max(0, self._cache_visible_count - len(pending))], len(pending)):
                raw_availability = str(
                    row.get("tier") or row.get("availability") or "none"
                ).lower()
                availability = {
                    "fulltext": text["fulltext"],
                    "abstract": text["abstract"],
                    "none": text["no_text"],
                }.get(raw_availability, raw_availability)
                key = self._cache_row_key(row)
                select_item = QtWidgets.QTableWidgetItem()
                select_item.setData(QtCore.Qt.ItemDataRole.UserRole, dict(row))
                select_item.setFlags(
                    QtCore.Qt.ItemFlag.ItemIsEnabled
                    | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                )
                select_item.setCheckState(
                    QtCore.Qt.CheckState.Checked if key in self._cache_checked_keys
                    else QtCore.Qt.CheckState.Unchecked
                )
                self.cache_table.setItem(index, 0, select_item)
                for column, value in enumerate((row.get("title") or row.get("paper") or row.get("raw_entry") or text["unavailable"], availability, row.get("updated_at") or row.get("updated") or text["unavailable"]), 1):
                    item = QtWidgets.QTableWidgetItem(str(value))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, dict(row))
                    if column == 2 and raw_availability == "abstract":
                        item.setToolTip(text["abstract_retry_tip"])
                    self.cache_table.setItem(index, column, item)
            self.cache_table.blockSignals(False)
            self._sync_cache_select_all()
            if "cache_page" not in self._loading_kinds or self.cache_table.rowCount():
                self.cache_stack.setCurrentIndex(0 if self.cache_table.rowCount() else 2)
            self._refresh_collection_presentation()

        def _refresh_collection_presentation(self):
            from .atlas_collections import refresh_rows
            refresh_rows(self, QtCore, QtGui, QtWidgets, text)

        def _selected_row(self, table):
            selected = table.selectedItems()
            return selected[0].data(QtCore.Qt.ItemDataRole.UserRole) if selected else None

        def _checked_history_rows(self) -> list[Mapping[str, Any]]:
            return [
                row for row in self._history_rows
                if str(row.get("run_dir") or "") in self._history_checked_run_dirs
            ]

        def _single_history_action_row(self):
            """Prefer one checked run; otherwise use one highlighted table row."""
            checked_rows = self._checked_history_rows()
            if len(checked_rows) == 1:
                return checked_rows[0]
            if checked_rows:
                return None
            row = self._selected_row(self.history_table)
            return row if isinstance(row, Mapping) else None

        def _history_item_changed(self, item):
            if item.column() != 0:
                return
            row = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(row, Mapping):
                return
            run_dir = str(row.get("run_dir") or "")
            if not run_dir:
                return
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                self._history_checked_run_dirs.add(run_dir)
            else:
                self._history_checked_run_dirs.discard(run_dir)
            self._sync_history_select_all()
            self._update_history_actions()

        def _remember_history_checkbox_state(self, table_row: int, column: int):
            item = self.history_table.item(table_row, column)
            self._history_checkbox_pressed_state = (
                table_row,
                item.checkState() if column == 0 and item is not None else None,
            )

        def _toggle_history_checkbox_cell(self, table_row: int, column: int):
            if column != 0:
                return
            item = self.history_table.item(table_row, column)
            if (
                item is None
                or not item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
            ):
                return
            pressed = self._history_checkbox_pressed_state
            self._history_checkbox_pressed_state = None
            if pressed == (table_row, item.checkState()):
                item.setCheckState(
                    QtCore.Qt.CheckState.Unchecked
                    if item.checkState() == QtCore.Qt.CheckState.Checked
                    else QtCore.Qt.CheckState.Checked
                )

        def _set_all_history_checked(self, state):
            if state == QtCore.Qt.CheckState.PartiallyChecked:
                self._sync_history_select_all()
                return
            checked = state == QtCore.Qt.CheckState.Checked
            self.history_table.blockSignals(True)
            self._history_checked_run_dirs = {
                str(row.get("run_dir") or "") for row in self._history_rows
                if checked and self._history_row_is_selectable(row)
            }
            for index in range(self.history_table.rowCount()):
                item = self.history_table.item(index, 0)
                if (
                    item is not None
                    and item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
                ):
                    item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
            self.history_table.blockSignals(False)
            self._update_history_actions()

        def _sync_history_select_all(self):
            total = sum(
                1 for row in self._history_rows
                if self._history_row_is_selectable(row)
            )
            checked = len(self._history_checked_run_dirs)
            self.history_select_all.blockSignals(True)
            self.history_select_all.setTristate(False)
            self.history_select_all.setCheckState(
                QtCore.Qt.CheckState.Checked if total and checked == total
                else QtCore.Qt.CheckState.Unchecked
            )
            self.history_select_all.blockSignals(False)

        @staticmethod
        def _cache_row_key(row: Mapping[str, Any]) -> str:
            return str(
                row.get("work_id") or row.get("parsed_text_id")
                or row.get("ref_id") or row.get("title")
                or row.get("paper") or id(row)
            )

        def _checked_cache_rows(self) -> list[Mapping[str, Any]]:
            return [
                row for row in getattr(self, "_cache_filtered_rows", [])
                if self._cache_row_key(row) in self._cache_checked_keys
            ]

        def _cache_delete_targets(self) -> list[Mapping[str, Any]]:
            checked = self._checked_cache_rows()
            if checked:
                return checked
            selected = self.cache_table.selectionModel().selectedRows(1)
            if len(selected) != 1:
                return []
            row = selected[0].data(QtCore.Qt.ItemDataRole.UserRole)
            return [row] if isinstance(row, Mapping) else []

        def _cache_item_changed(self, item):
            if item.column() != 0:
                return
            row = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(row, Mapping):
                return
            key = self._cache_row_key(row)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                self._cache_checked_keys.add(key)
            else:
                self._cache_checked_keys.discard(key)
            self._sync_cache_select_all()

        def _remember_cache_checkbox_state(self, table_row: int, column: int):
            item = self.cache_table.item(table_row, column)
            self._cache_checkbox_pressed_state = (
                table_row,
                item.checkState() if column == 0 and item is not None else None,
            )

        def _toggle_cache_checkbox_cell(self, table_row: int, column: int):
            if column != 0:
                return
            item = self.cache_table.item(table_row, column)
            if (
                item is None
                or not item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
            ):
                return
            pressed = self._cache_checkbox_pressed_state
            self._cache_checkbox_pressed_state = None
            if pressed == (table_row, item.checkState()):
                item.setCheckState(
                    QtCore.Qt.CheckState.Unchecked
                    if item.checkState() == QtCore.Qt.CheckState.Checked
                    else QtCore.Qt.CheckState.Checked
                )

        def _set_all_cache_checked(self, state):
            if state == QtCore.Qt.CheckState.PartiallyChecked:
                self._sync_cache_select_all()
                return
            checked = state == QtCore.Qt.CheckState.Checked
            self.cache_table.blockSignals(True)
            for row in self._cache_filtered_rows:
                key = self._cache_row_key(row)
                if checked:
                    self._cache_checked_keys.add(key)
                else:
                    self._cache_checked_keys.discard(key)
            for index in range(self.cache_table.rowCount()):
                item = self.cache_table.item(index, 0)
                if item is not None and item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
            self.cache_table.blockSignals(False)
            self._sync_cache_select_all()

        def _sync_cache_select_all(self):
            filtered = getattr(self, "_cache_filtered_rows", [])
            total = len(filtered)
            checked = sum(1 for row in filtered if self._cache_row_key(row) in self._cache_checked_keys)
            self.cache_select_all.blockSignals(True)
            self.cache_select_all.setTristate(False)
            self.cache_select_all.setCheckState(
                QtCore.Qt.CheckState.Checked if total and checked == total
                else QtCore.Qt.CheckState.Unchecked
            )
            self.cache_select_all.blockSignals(False)
            targets = self._cache_delete_targets()
            self.cache_delete_button.setEnabled(bool(targets))
            self.cache_delete_button.setToolTip(
                text["delete_selected"] if targets else text["library_select_for_delete"]
            )

        @staticmethod
        def _history_row_is_selectable(row: Mapping[str, Any]) -> bool:
            return bool(
                str(row.get("run_dir") or "")
                and (
                    row.get("run_status") != "active"
                    or row.get("crash_recoverable") is True
                )
            )

        @staticmethod
        def _history_row_is_resumable(row: Mapping[str, Any]) -> bool:
            return (
                row.get("available", True) is True
                and (
                    row.get("run_status") != "active"
                    or row.get("crash_recoverable") is True
                )
                and row.get("run_status") != "completed"
                and not bool(row.get("completed"))
            )

        @staticmethod
        def _history_row_has_parse_review(row: Mapping[str, Any]) -> bool:
            return bool(
                row.get("available", True) is True
                and row.get("phase") == "parse"
                and row.get("run_status") == "paused"
            )

        def _history_row_is_deletable(self, row: Mapping[str, Any]) -> bool:
            run_dir = str(row.get("run_dir") or "")
            guided_run_dir = str(getattr(self._guided_window, "_run_dir", "") or "")
            protected = (
                run_dir == str(self._run_dir or "")
                and (
                    self._process is not None
                    or self._commands_process is not None
                    or self._guided_window is not None
                    or self._institutional_launch_request is not None
                    or self._library_busy
                )
            ) or (
                run_dir == guided_run_dir
            ) or (
                self._parse_review_window is not None
                and run_dir == self._parse_review_run_dir
            )
            return bool(
                run_dir
                and row.get("run_status") != "active"
                and not protected
            )

        @staticmethod
        def _history_row_is_verify_forkable(row: Mapping[str, Any]) -> bool:
            return (
                row.get("available", True) is True
                and row.get("run_status") == "completed"
                and row.get("phase") == "done"
                and bool(row.get("completed", True))
            )

        @staticmethod
        def _history_row_is_exportable(row: Mapping[str, Any]) -> bool:
            path = row.get("report_html_path")
            return bool(
                row.get("report_html_present") is True
                and isinstance(path, str)
                and Path(path).name == "report.html"
                and str(row.get("run_dir") or "")
            )

        def _update_history_actions(self):
            checked_rows = self._checked_history_rows()
            row = self._single_history_action_row()
            institutional_preflight_pending = (
                self._institutional_launch_request is not None
            )
            preview_selected = isinstance(row, Mapping) and bool(row.get("references_only"))
            self.history_resume_button.setText(
                text["resume"]
            )
            self.history_verify_fork_button.setText(
                text["continue_fetch_verify"] if preview_selected else text["rerun_verify"]
            )
            self.history_resume_button.setEnabled(
                isinstance(row, Mapping)
                and self._history_row_is_resumable(row)
                and self._commands_process is None
                and self._guided_window is None
                and self._parse_review_window is None
                and not institutional_preflight_pending
            )
            show_parse_review = isinstance(row, Mapping) and self._history_row_has_parse_review(row)
            self.history_parse_review_button.setVisible(show_parse_review)
            self.history_parse_review_button.setText(
                text["open_parse_review"]
                if isinstance(row, Mapping) and row.get("parse_review_pending")
                else text["resume"]
            )
            parse_review_window_reopenable = (
                self._parse_review_window is None
                or (
                    isinstance(row, Mapping)
                    and self._parse_review_window.isHidden()
                    and self._parse_review_run_dir == str(row.get("run_dir") or "")
                )
            )
            self.history_parse_review_button.setEnabled(
                show_parse_review
                and self._process is None
                and self._commands_process is None
                and self._guided_window is None
                and not institutional_preflight_pending
                and parse_review_window_reopenable
            )
            self.history_report_button.setEnabled(
                isinstance(row, Mapping) and row.get("available", True) is True
            )
            self.history_open_report_button.setEnabled(
                isinstance(row, Mapping)
                and bool(row.get("report_html_present"))
                and isinstance(row.get("report_html_path"), str)
            )
            report_path = str(row.get("report_html_path") or "") if isinstance(row, Mapping) else ""
            run_directory = str(row.get("run_dir") or "") if isinstance(row, Mapping) else ""
            self.history_report_folder_button.setEnabled(
                (bool(report_path) and self.history_open_report_button.isEnabled()
                 and Path(report_path).is_file())
                or (bool(run_directory) and Path(run_directory).is_dir()))
            highlighted_row = self._selected_row(self.history_table)
            self.history_delete_run_button.setEnabled(
                isinstance(highlighted_row, Mapping)
                and self._history_row_is_deletable(highlighted_row)
            )
            self.history_export_report_button.setEnabled(
                export_report_callback is not None
                and isinstance(row, Mapping)
                and self._history_row_is_exportable(row)
            )
            historical_export = (
                isinstance(row, Mapping) and row.get("available", True) is False
            )
            self.history_export_report_button.setText(
                text["export_historical_report"] if historical_export
                else text["export_report"]
            )
            self.history_export_report_button.setToolTip(
                text["export_historical_report_tip"] if historical_export
                else text["export_report_tip"]
            )
            self.history_verify_fork_button.setEnabled(
                isinstance(row, Mapping)
                and self._history_row_is_verify_forkable(row)
                and self._process is None
                and self._commands_process is None
                and self._guided_window is None
                and self._parse_review_window is None
                and not institutional_preflight_pending
            )
            deletable = [
                candidate for candidate in checked_rows
                if self._history_row_is_deletable(candidate)
            ]
            self.history_delete_button.setEnabled(bool(checked_rows) and len(deletable) == len(checked_rows))

            self._refresh_collection_presentation()

        def _open_history_row(self, table_row: int, _column: int):
            if _column == 0:
                return
            item = self.history_table.item(table_row, 0)
            row = (
                item.data(QtCore.Qt.ItemDataRole.UserRole)
                if item is not None else None
            )
            if isinstance(row, Mapping):
                self._open_history_report(row, fallback_to_run=True)

        def _open_selected_history_report(self):
            row = self._single_history_action_row()
            if isinstance(row, Mapping):
                self._open_history_report(row, fallback_to_run=False)

        def _open_selected_report_folder(self):
            row = self._single_history_action_row()
            if not isinstance(row, Mapping):
                return
            if row.get("report_html_present"):
                report = Path(str(row.get("report_html_path") or ""))
                if report.is_file():
                    self._open_path(str(report.resolve().parent))
                    return
            run_directory = str(row.get("run_dir") or "")
            if run_directory and Path(run_directory).is_dir():
                self._open_path(str(Path(run_directory).resolve()))

        def _export_selected_history_report(self):
            row = self._single_history_action_row()
            if isinstance(row, Mapping) and self._history_row_is_exportable(row):
                self._export_report_for_run(
                    str(row["run_dir"]),
                    historical=row.get("available", True) is False,
                )

        def _open_history_report(self, row: Mapping[str, Any], *, fallback_to_run: bool):
            report_path = row.get("report_html_path")
            if row.get("report_html_present") and isinstance(report_path, str):
                self._open_path(report_path)
                return
            if fallback_to_run:
                run_dir = row.get("run_dir")
                if isinstance(run_dir, str) and run_dir:
                    self._open_path(run_dir)

        def _resume_selected_history(self):
            if (
                self._institutional_launch_request is not None
                or self._guided_window is not None
            ):
                return
            row = self._single_history_action_row()
            if not isinstance(row, Mapping):
                return
            run_dir = row.get("run_dir")
            if not isinstance(run_dir, str):
                return
            try:
                current_rows = ([history_detail_loader(run_dir)]
                                if history_page_loader is not None and history_detail_loader is not None
                                else list(history_loader() or ()))
            except (OSError, ValueError):
                return
            matches = [
                current_row
                for current_row in current_rows
                if isinstance(current_row, Mapping) and current_row.get("run_dir") == run_dir
            ]
            if len(matches) != 1 or not self._history_row_is_resumable(matches[0]):
                return
            current_row = matches[0]
            self._resume_run_row(current_row)

        def _open_selected_history_parse_review(self):
            if (
                self._institutional_launch_request is not None
                or self._guided_window is not None
            ):
                return
            row = self._single_history_action_row()
            if not isinstance(row, Mapping) or not self._history_row_has_parse_review(row):
                return
            run_dir = row.get("run_dir")
            if not isinstance(run_dir, str) or not run_dir:
                return
            if (
                self._parse_review_window is not None
                and self._parse_review_run_dir != run_dir
            ):
                return
            self.set_run_dir(run_dir)
            self.navigation.setCurrentRow(0)
            self._open_parse_review(run_dir)

        def _regenerate_report(self):
            row = self._single_history_action_row()
            if isinstance(row, Mapping) and report_callback is not None:
                result = report_callback(row)
                if result:
                    command, _ignored, environment = _command_spec(result)
                    if command:
                        self._start_process(
                            command, environment=environment, auto_open_report=False
                        )

        def _select_verify_fork_roles(
            self, options: Sequence[Mapping[str, Any]]
        ) -> dict[str, list[str]] | None:
            if not self._begin_jury_action_selection():
                return None
            available = list(options)
            known = {
                str(item.get("selector") or "") for item in available
                if isinstance(item, Mapping)
            }
            for selector in self.llm_role_choices:
                if selector in known:
                    continue
                available.append({
                    "selector": selector,
                    "label": self._llm_option_labels.get(selector, selector),
                })
                known.add(selector)
            if verify_fork_selector is not None:
                selected = verify_fork_selector(available)
                if selected is None:
                    self._finish_jury_action_selection(confirmed=False)
                    return None
                if not isinstance(selected, Mapping):
                    self._finish_jury_action_selection(confirmed=False)
                    return None
                normalized = {
                    role: list(dict.fromkeys(
                        str(item).strip() for item in selected.get(role, ())
                        if str(item).strip()
                    ))
                    for role in ("jury1", "jury2")
                }
                self._set_llm_role_selection(normalized)
                if not any(normalized.values()):
                    self._finish_jury_action_selection(confirmed=False)
                    return None
                preview = self._selected_llm_preview()
                if verify_selection_preview_enabled and not preview.get("available"):
                    self._finish_jury_action_selection(confirmed=False)
                    return None
                if not self._finish_jury_action_selection(
                    confirmed=True, preview=preview
                ):
                    return None
                return normalized

            dialog = QtWidgets.QDialog(self)
            dialog.setObjectName("verifyForkRoleDialog")
            dialog.setWindowTitle(text["rerun_verify"])
            dialog.setStyleSheet(self.styleSheet())
            dialog.setFont(self.font())
            dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
            bounds = self.screen().availableGeometry()
            max_width = max(1, min(bounds.width() - 32, self.width() - 32))
            max_height = max(1, min(bounds.height() - 32, self.height() - 32))
            dialog.setMaximumSize(max_width, max_height)
            dialog.resize(min(600, max_width), min(300, max_height))
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.setContentsMargins(16, 16, 16, 16)
            scroll = QtWidgets.QScrollArea(dialog)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            body = QtWidgets.QWidget(scroll)
            body_layout = QtWidgets.QVBoxLayout(body)
            body_layout.setContentsMargins(0, 0, 0, 0)
            explanation = QtWidgets.QLabel(text["select_verify_backends"], body)
            explanation.setWordWrap(True)
            body_layout.addWidget(explanation)
            role_buttons = QtWidgets.QVBoxLayout()
            self._jury_action_dialog = dialog
            self._jury_action_role_buttons = {}
            for role in ("jury1", "jury2"):
                button = QtWidgets.QPushButton(dialog)
                button.setObjectName(f"verifyFork{role.title()}Button")
                button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
                button.installEventFilter(self)
                button.clicked.connect(
                    lambda _checked=False, role=role: self._show_jury_models(
                        role, modal_parent=dialog
                    )
                )
                self._jury_action_role_buttons[role] = button
                role_buttons.addWidget(button)
            body_layout.addLayout(role_buttons)
            body_layout.addStretch(1)
            scroll.setWidget(body)
            layout.addWidget(scroll, 1)
            self._refresh_jury_action_role_buttons()
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Ok
                | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
                parent=dialog,
            )
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText(
                text["verify_fork_continue"]
            )
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText(text["atlas_cancel"])
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            buttons.ensurePolished()
            margins = layout.contentsMargins()
            if buttons.minimumSizeHint().width() + margins.left() + margins.right() > max_width:
                buttons.setOrientation(QtCore.Qt.Orientation.Vertical)
            accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
            self._jury_action_dialog = None
            self._jury_action_role_buttons = None
            if not accepted:
                self._finish_jury_action_selection(confirmed=False)
                return None
            selected = self._selected_llm_roles()
            valid_selectors = {
                str(item.get("selector") or "") for item in available
            } | set(self.llm_role_choices)
            if (
                not any(selected.values())
                or any(selector not in valid_selectors for values in selected.values() for selector in values)
            ):
                self._finish_jury_action_selection(confirmed=False)
                self._show_provider_check_warning(text["jury_readiness_invalid"])
                return None
            preview = self._selected_llm_preview()
            if verify_selection_preview_enabled and not preview.get("available"):
                self._finish_jury_action_selection(confirmed=False)
                self._show_provider_check_warning(text["jury_readiness_invalid"])
                return None
            if not self._finish_jury_action_selection(
                confirmed=True, preview=preview
            ):
                return None
            return selected

        def _fork_selected_history_verify(self):
            if (
                self._institutional_launch_request is not None
                or self._guided_window is not None
            ):
                return
            if self._parse_review_window is not None:
                self._parse_review_window.raise_()
                self._parse_review_window.activateWindow()
                return
            if self._commands_process is not None:
                QtWidgets.QMessageBox.warning(
                    self, text["tab_history"], text["commands_status_busy_commands"]
                )
                return
            if self._process is not None:
                QtWidgets.QMessageBox.warning(
                    self, text["tab_history"], text["verify_fork_process_active"]
                )
                return
            selected_row = self._single_history_action_row()
            if not isinstance(selected_row, Mapping):
                return
            selected_run_dir = str(selected_row.get("run_dir") or "")
            try:
                current_row = next(
                    (
                        row for row in (history_loader() or ())
                        if str(row.get("run_dir") or "") == selected_run_dir
                    ),
                    None,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(
                    self, text["tab_history"], str(exc)
                )
                return
            if (
                not isinstance(current_row, Mapping)
                or not self._history_row_is_verify_forkable(current_row)
            ):
                self.history_verify_fork_button.setEnabled(False)
                QtWidgets.QMessageBox.warning(
                    self, text["tab_history"], text["verify_fork_unavailable"]
                )
                return
            try:
                options = list(verify_fork_options_loader() or ())
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(
                    self, text["tab_history"], str(exc)
                )
                return
            selected_roles = self._select_verify_fork_roles(options)
            if not selected_roles:
                return
            if verify_fork_command_builder is None:
                return
            try:
                result = verify_fork_command_builder(
                    current_row, selected_roles, dict(self._llm_catalogs)
                )
                command, run_dir, environment = _command_spec(result)
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(
                    self, text["tab_history"], str(exc)
                )
                return
            if command:
                if run_dir:
                    if environment:
                        self._run_environment_by_dir[run_dir] = dict(environment)
                    if run_dir != selected_run_dir:
                        self.set_run_dir(selected_run_dir)
                    self._pending_child_source_dir = run_dir if run_dir != selected_run_dir else None
                    self.set_run_dir(run_dir)
                self.navigation.setCurrentRow(0)
                readiness_level = str(
                    (environment or {}).get(
                        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL",
                        self._selected_jury_level(),
                    )
                )
                self._start_process(
                    command,
                    environment=environment,
                    readiness_selection=(selected_roles, readiness_level),
                )

        def _delete_selected_cache(self):
            rows = self._cache_delete_targets()
            confirmed = rows and self._confirm_library_action(text["confirm_delete_cache"])
            if confirmed and delete_cache_callback is not None:
                try:
                    delete_cache_callback(rows)
                except (OSError, RuntimeError, ValueError) as exc:
                    QtWidgets.QMessageBox.warning(
                        self, text["tab_cache"], str(exc)
                    )
                else:
                    self._refresh_cache_after_mutation()

        def _delete_selected_history(self):
            rows = self._checked_history_rows()
            if not rows or any(
                not self._history_row_is_deletable(row)
                for row in rows
            ):
                return
            self._delete_history_rows(rows)

        def _delete_selected_history_run(self):
            row = self._selected_row(self.history_table)
            if not isinstance(row, Mapping) or not self._history_row_is_deletable(row):
                return
            self._delete_history_rows([row])

        def _delete_history_rows(self, rows: list[Mapping[str, Any]]):
            if not rows or any(
                not self._history_row_is_deletable(row) for row in rows
            ):
                return
            current_run_deleted = any(
                str(row.get("run_dir") or "") == str(self._run_dir or "")
                for row in rows
            )
            confirmed = self._confirm_library_action(
                text["confirm_delete_runs"],
                title=text["tab_history"],
                icon_name="trash",
            )
            if not confirmed:
                return
            if any(not self._history_row_is_deletable(row) for row in rows):
                return
            try:
                delete_history_callback(rows)
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, text["tab_history"], str(exc))
            else:
                self._history_checked_run_dirs.clear()
                if (
                    current_run_deleted
                    and self._process is None
                    and self._commands_process is None
                    and self._guided_window is None
                    and self._parse_review_window is None
                    and self._institutional_launch_request is None
                ):
                    self._clear_analysis()
                self._refresh_history_after_mutation()

        def _clear_cache(self):
            confirmed = self._confirm_library_action(text["confirm_clear_cache"])
            if confirmed and clear_cache_callback is not None:
                try:
                    clear_cache_callback()
                except (OSError, RuntimeError, ValueError) as exc:
                    QtWidgets.QMessageBox.warning(
                        self, text["tab_cache"], str(exc)
                    )
                else:
                    self._refresh_cache_after_mutation()

        def _open_parse_review(self, run_dir: str):
            if (
                self._process is not None
                or self._commands_process is not None
                or self._guided_window is not None
                or self._institutional_launch_request is not None
            ):
                return
            if self._parse_review_window is not None:
                if self._parse_review_run_dir != run_dir:
                    return
                if getattr(self._parse_review_window, "_atlas_embedded", False):
                    self.navigation.setCurrentRow(0)
                if self._parse_review_window.isHidden():
                    self._embed_parse_review(self._parse_review_window)
                    self._parse_review_window.show()
                self._parse_review_window.raise_()
                self._parse_review_window.activateWindow()
                self._update_run_controls()
                self._update_history_actions()
                return
            controller_factory = parse_review_controller_factory
            if controller_factory is None:
                from core.app.parse_review import ParseReviewController
                controller_factory = ParseReviewController
            try:
                controller = controller_factory(run_dir)
                if controller.pending_count() == 0:
                    self._continue_parse_review(run_dir)
                    return
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
                QtWidgets.QMessageBox.warning(self, text["open_parse_review"], str(exc))
                return

            def complete():
                if controller.pending_count() != 0:
                    raise ValueError(text["parse_review_pending_warning"])
                self._continue_parse_review(run_dir)

            def review_dismissed():
                if self._load_closed:
                    return
                review = self._parse_review_window
                if review is not None and not review.isVisible():
                    self._restore_analysis_workspace(review)
                self._update_run_controls()
                self._update_history_actions()

            factory = parse_review_window_factory
            if factory is None:
                from .parse_review import create_parse_review_window as factory
            self._parse_review_window = factory(
                run_dir,
                load_tasks=controller.load_tasks,
                answer_task=controller.submit_decision,
                skip_remaining=lambda: controller.skip_remaining_reviews(),
                on_complete=complete,
                on_dismiss=review_dismissed,
                review_text=text,
            )
            self._parse_review_run_dir = run_dir
            self._embed_parse_review(self._parse_review_window)
            if hasattr(self._parse_review_window, "set_dark_theme"):
                self._parse_review_window.set_dark_theme(self._dark)
            self._parse_review_window.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False
            )
            self._parse_review_window.show()
            self.phase_label.setText(text["parse_review_waiting"])
            self._update_run_controls()
            self._update_history_actions()

        def _apply_institutional_snapshot(self, value: Any):
            if not isinstance(value, Mapping):
                if not self._institutional_session_prepared:
                    self.institutional_access_status.setText(
                        text["institutional_access_optional"]
                    )
                return
            states = [
                item for item in value.get("states", ())
                if isinstance(item, Mapping)
            ]
            state_names = {str(item.get("state") or "") for item in states}
            domains = list(dict.fromkeys(
                str(item.get("domain") or "") for item in states
                if item.get("domain")
            ))
            domain = ", ".join(domains)
            if value.get("status") == "checking":
                message = text["institutional_access_checking"]
            elif "login_required" in state_names:
                message = text["institutional_access_login_required"].format(domain=domain)
            elif "challenge" in state_names:
                message = text["institutional_access_challenge"].format(domain=domain)
            elif "validation_required" in state_names:
                message = text["institutional_access_validation_required"].format(domain=domain)
            elif "access_unavailable" in state_names or value.get("status") == "unavailable":
                message = text["institutional_access_unavailable"]
            elif "unsupported" in state_names:
                message = text["institutional_access_unsupported"].format(domain=domain)
            elif "validated" in state_names:
                message = text["institutional_access_validated"].format(domain=domain)
            elif self._institutional_session_prepared:
                message = text["institutional_session_prepared"].format(
                    profile=value.get("profile") or "guided_fetch"
                )
            else:
                message = text["institutional_access_optional"]
            self.institutional_access_status.setText(message)

        def _continue_parse_review(self, run_dir: str):
            command, _ignored, environment = _command_spec(
                resume_command_builder(run_dir)
            )
            if not command:
                raise ValueError(text["resume_latest_no_history"])
            environment = self._resume_environment(run_dir, environment)
            window = (
                self._parse_review_window
                if self._parse_review_run_dir == run_dir else None
            )
            if window is not None:
                self._restore_analysis_workspace(window)
                self._parse_review_window = None
                self._parse_review_run_dir = None
                # Resume is called from the dialog's own button handler. Qt
                # must finish dispatching that handler before destroying it.
                self._retiring_parse_review_window = window
                window.hide()
                window.destroyed.connect(
                    lambda *_args: QtCore.QTimer.singleShot(
                        0, lambda: setattr(self, "_retiring_parse_review_window", None)
                    )
                )
                window.deleteLater()
            self._start_process(command, environment=environment)
            self._update_run_controls()
            self._update_history_actions()

        def _open_guided_fetch(self, run_dir: str):
            if self._commands_process is not None:
                self._set_commands_status(
                    text["commands_status_busy_commands"], error=True
                )
                return
            if self._guided_window is not None:
                if getattr(self._guided_window, "_atlas_embedded", False):
                    self.navigation.setCurrentRow(0)
                self._guided_window.raise_()
                self._guided_window.activateWindow()
                return
            factory = guided_window_factory
            controller = guided_controller_factory(run_dir) if guided_controller_factory else None
            if factory is None:
                from .guided_fetch import create_guided_fetch_window as factory
            if controller is None:
                from core.app.guided_fetch import GuidedFetchController
                controller = GuidedFetchController(run_dir)

            def prepare_proceed(*, skip_remaining_confirmed: bool = False):
                controller.proceed(
                    skip_remaining_confirmed=skip_remaining_confirmed
                )

            def proceed():
                self._fetch_cache_clear_pending = self._browser_cache_policy() == "fetch"
                command, _ignored_run_dir, environment = _command_spec(
                    resume_command_builder(run_dir)
                )
                if command:
                    self._start_process(
                        command,
                        environment=self._resume_environment(run_dir, environment),
                    )

            def on_ocr_event(event):
                ref_id = event.get("ref_id") if isinstance(event, dict) else None
                status = event.get("status") if isinstance(event, dict) else None
                if not isinstance(ref_id, str) or status not in {"queued", "running", "done", "failed"}:
                    return
                reason = str(event.get("reason") or "").strip()
                suffix = f": {reason}" if reason else ""
                self._append_console_output(f"OCR [{ref_id}] {status}{suffix}\n")

            guided_window = factory(
                run_dir,
                submit_source=controller.stage_source,
                proceed=proceed,
                prepare_proceed=prepare_proceed,
                discard_source=controller.discard_source,
                run_ocr=getattr(controller, "run_ocr", None),
                on_ocr_event=on_ocr_event,
                ocr_text=text,
                submit_identity_review=controller.submit_identity_decision,
                identity_review_allowed=controller.identity_review_allowed,
            )
            self._guided_window = guided_window
            if isinstance(guided_window, QtWidgets.QMainWindow):
                self._embed_guided_fetch(guided_window)
            source_stage_idle = getattr(guided_window, "source_stage_idle", None)
            if source_stage_idle is not None:
                source_stage_idle.connect(
                    lambda: self._retry_institutional_when_idle()
                    if not self._load_closed and self._guided_window is guided_window else None
                )
            guided_window.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True
            )
            def guided_closed(*_args):
                if not self._load_closed and getattr(guided_window, "_atlas_embedded", False):
                    self._move_phase_rail(
                        self._atlas_heading_row, self._atlas_analysis_workspace
                    )
                    self._atlas_analysis_workspace.show()
                if self._guided_window is guided_window:
                    self._guided_window = None
                if self._fetch_cache_clear_pending and not self._load_closed:
                    self._fetch_cache_clear_pending = False
                    QtCore.QTimer.singleShot(0, self._clear_browser_cache)
                if self._load_closed:
                    return
                self._update_run_controls()
                self._update_history_actions()
                if self._institutional_retry_pending == run_dir:
                    QtCore.QTimer.singleShot(
                        0, lambda: self._retry_institutional_when_idle()
                        if not self._load_closed else None,
                    )
            guided_window.destroyed.connect(guided_closed)
            guided_window.show()
            self.phase_label.setText(text["guided_fetch_waiting"])
            self._update_run_controls()
            self._update_history_actions()

        def _move_phase_rail(self, destination, parent=None):
            # Detach explicitly: implicit QLayout transfers leave a borrowed
            # QWidgetItem alive in the binding cache after Qt deletes it.
            old_parent = self._atlas_steps.parentWidget()
            if old_parent is not None:
                for layout in old_parent.findChildren(QtWidgets.QLayout):
                    if layout.indexOf(self._atlas_steps) >= 0:
                        layout.removeWidget(self._atlas_steps)
                        break
            if parent is not None:
                self._atlas_steps.setParent(parent)
            destination.addWidget(self._atlas_steps, 2)

        def _restore_analysis_workspace(self, review):
            if not getattr(review, "_atlas_embedded", False):
                return
            self._move_phase_rail(
                self._atlas_heading_row, self._atlas_analysis_workspace
            )
            review.hide()
            self._atlas_analysis_workspace.show()

        def _embed_parse_review(self, review):
            # Injected dialog factories keep their own presentation contract.
            if not hasattr(review, "task_tabs") or not hasattr(review, "detail_layout"):
                return
            self.navigation.setCurrentRow(0)
            if not getattr(review, "_atlas_embedded", False):
                page = self.pages.widget(0)
                review.setParent(page)
                review.setWindowFlags(QtCore.Qt.WindowType.Widget)
                review._atlas_embedded = True
                review.setMinimumSize(0, 0)
                layout = review.layout()
                layout.setContentsMargins(30, 26, 30, 0)
                heading = QtWidgets.QWidget(review)
                row = QtWidgets.QHBoxLayout(heading)
                row.setContentsMargins(0, 0, 0, 0)
                title = QtWidgets.QLabel(text["atlas_parse_title"], heading)
                title.setObjectName("reviewPageTitle")
                title.setWordWrap(True)
                row.addWidget(title, 1)
                review._atlas_heading_row = row
                layout.insertWidget(0, heading)
                manuscript = QtWidgets.QLabel(
                    Path(getattr(self, "_paper_path", "") or self._run_dir or "").name, review
                )
                manuscript.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                manuscript.setObjectName("pageSubtitle")
                layout.insertWidget(1, manuscript)
                page.layout().addWidget(review, 1)
            review._font_size = self._font_size
            self._move_phase_rail(review._atlas_heading_row)
            self._atlas_analysis_workspace.hide()

        def _embed_guided_fetch(self, guided_window):
            """Keep assisted retrieval in the shared analysis frame."""
            self.navigation.setCurrentRow(0)
            page = self.pages.widget(0)
            guided_window.setParent(page)
            guided_window.setWindowFlags(QtCore.Qt.WindowType.Widget)
            guided_window._atlas_embedded = True
            guided_window.setMinimumSize(0, 0)
            brand = guided_window.findChild(QtWidgets.QFrame, "brandHeader")
            if brand is not None:
                brand.hide()
            layout = guided_window.centralWidget().layout()
            layout.setContentsMargins(30, 26, 30, 0)
            heading = QtWidgets.QWidget(guided_window)
            heading_layout = QtWidgets.QHBoxLayout(heading)
            heading_layout.setContentsMargins(0, 0, 0, 0)
            title = QtWidgets.QLabel(text["atlas_guided_title"], heading)
            title.setObjectName("pageTitle")
            heading_layout.addWidget(title, 1)
            self._move_phase_rail(heading_layout)
            layout.insertWidget(0, heading)
            manuscript = QtWidgets.QLabel(
                Path(getattr(self, "_paper_path", "") or self._run_dir or "").name, guided_window
            )
            manuscript.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            manuscript.setObjectName("pageSubtitle")
            layout.insertWidget(1, manuscript)
            page.layout().addWidget(guided_window, 1)
            self._atlas_analysis_workspace.hide()
            guided_window._font_size_px = self._font_size
            guided_window._set_dark_theme(self._dark)

        def closeEvent(self, event):
            if self._report_export_cancel_event is not None:
                self._report_export_close_pending = True
                self._cancel_report_export()
                event.ignore()
                return
            if self._loading_kinds.intersection({"settings_transfer", "settings_inspect"}):
                self.statusBar().showMessage(text["settings_transfer_working"])
                event.ignore()
                return
            if self._library_busy:
                event.ignore()
                return
            guided_window = self._guided_window
            source_stage_busy = getattr(guided_window, "_source_stage_busy", None)
            if guided_window is not None and (
                getattr(guided_window, "_proceed_preparing", False)
                or getattr(guided_window, "_proceed_thread", None) is not None
                or (callable(source_stage_busy) and source_stage_busy())
            ):
                self.statusBar().showMessage(text["guided_fetch_waiting"], 5000)
                event.ignore()
                return
            if self._institutional_dialog is not None:
                event.ignore()
                self._institutional_close_pending = True
                self._institutional_launch_request = None
                self._institutional_retry_pending = None
                self._institutional_login_deferred = False
                self._institutional_dialog.close()
                return
            if self._commands_process is not None:
                QtWidgets.QMessageBox.warning(
                    self,
                    text["app_title"],
                    text["commands_running_close"],
                )
                event.ignore()
                return
            if self._process is not None:
                QtWidgets.QMessageBox.warning(
                    self,
                    text["app_title"],
                    text["analysis_running_close"],
                )
                event.ignore()
                return
            if self._settings_dirty:
                decision = self._confirm_settings_changes()
                if decision == "cancel":
                    event.ignore()
                    return
                if decision == "save" and not self._save_settings():
                    event.ignore()
                    return
                if decision == "discard":
                    self._discard_settings()
            if guided_window is not None:
                event.ignore()
                if getattr(guided_window, "_ocr_thread", None) is not None:
                    guided_window.close()
                    return
                if not getattr(self, "_close_after_guided", False):
                    self._close_after_guided = True
                    guided_window.destroyed.connect(lambda *_args: QtCore.QTimer.singleShot(0, self.close))
                    guided_window.close()
                return
            if self._browser_cache_policy() == "close" and not self._browser_cache_exit_done:
                self._browser_cache_close_pending = True
                event.ignore()
                self._clear_browser_cache()
                return
            if self._restart_language is not None:
                QtCore.QSettings("Callimachus", "Callimachus").sync()
                command = application_argv("app", "--language", self._restart_language)
                try:
                    started = QtCore.QProcess.startDetached(command[0], command[1:], str(resource_root()))
                    started = started[0] if isinstance(started, tuple) else started
                except (OSError, RuntimeError):
                    started = False
                self._restart_language = None
                if not started:
                    QtWidgets.QMessageBox.warning(self, text["tab_settings"], text["language_restart_failed"])
                    event.ignore()
                    return
            self._hide_settings_search_results()
            self._load_closed = True
            if self._load_pool is not None:
                for kind in tuple(self._load_futures):
                    self._invalidate_load(kind)
                self._load_pool.shutdown(wait=False, cancel_futures=True)
            if self._library_pool is not None:
                self._library_pool.shutdown(wait=False, cancel_futures=True)
            self._cleanup_console_capture()
            self._atlas_window_chrome.uninstall()
            super().closeEvent(event)

    return DesktopWindow()


def run_desktop(**kwargs: Any) -> int:
    """Start the optional desktop shell in the process owning QApplication."""
    _, _, QtWidgets = _load_qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    kwargs.setdefault("background_loading", True)
    window = create_desktop_window(**kwargs)
    window.show()
    startup = getattr(app, "_callimachus_startup_session", None)
    if startup is not None:
        startup.window_ready(window)
        return 0
    return app.exec()


def _load_settings_environment_preferences() -> list[str]:
    """Remember explicit GUI replacement without changing CLI precedence."""
    try:
        from PySide6.QtCore import QSettings
        values = QSettings("Callimachus", "Callimachus").value("settings_environment_names", [])
    except ModuleNotFoundError:
        return []
    if not isinstance(values, list) or len(values) > 1024:
        return []
    from core.app.settings.config import _dotenv_values
    current_names = _dotenv_values(settings_template_file(source_root=resource_root()))
    return [name for name in values if isinstance(name, str) and name in current_names]


def _load_language_preference() -> str | None:
    """Keep GUI preferences outside the pipeline environment boundary."""
    try:
        from PySide6.QtCore import QSettings
        value = QSettings("Callimachus", "Callimachus").value("language")
        return str(value) if value in {"en", "it"} else None
    except ModuleNotFoundError:
        return None


def _save_language_preference(language: str) -> None:
    if language not in {"en", "it"}:
        return
    try:
        from PySide6.QtCore import QSettings
        QSettings("Callimachus", "Callimachus").setValue("language", language)
    except ModuleNotFoundError:
        return


def _load_qt():
    from PySide6 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def _wordmark(QtCore, QtGui, width: int, height: int, ratio: float, *, dark: bool = False):
    name = "logo-dark.png" if dark else "logo.png"
    path = Path(__file__).resolve().parent / "assets" / name
    source = QtGui.QPixmap(str(path))
    if source.isNull():
        return source
    # Trim the transparent padding in the official 2400x576 raster asset.
    source = source.copy(QtCore.QRect(145, 109, 2196, 360))
    pixmap = source.scaled(
        max(1, round(width * ratio)), max(1, round(height * ratio)),
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def _spinner_icon(QtCore, QtGui, frame: int, *, color: str = "#ffffff"):
    from .atlas_icons import _svg_engine
    asset = Path(__file__).parent / "assets" / "atlante" / "loading.svg"
    svg = asset.read_text(encoding="utf-8").replace("currentColor", color)
    svg = svg.replace("rotate(0 12 12)", f"rotate({frame % 12 * 30} 12 12)")
    return QtGui.QIcon(_svg_engine(QtCore, QtGui, svg))


def _safe_activity_text(payload: bytes, limit: int = 280) -> str:
    """Keep child output diagnostic-only and avoid displaying credentials."""
    raw = re.sub(
        r"\x1b\[[0-?]*[ -/]*[@-~]", "",
        payload.decode("utf-8", errors="replace"),
    )
    lines = [
        line for line in raw.splitlines()
        if line.strip() and not _is_activity_separator(line)
    ]
    if not lines:
        return ""
    value = lines[-1]
    value = re.sub(
        r"(?i)(\b(?:api[_-]?key|token|secret|password|auth(?:orization)?)\b\s*[:=]\s*)\S+",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"(?i)([?&](?:api[_-]?key|token|secret|password|auth(?:orization)?)=)[^&#\s]+",
        r"\1[redacted]",
        value,
    )
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _is_activity_separator(line: str) -> bool:
    """Ignore log decoration without discarding ordinary punctuation in text."""
    value = "".join(line.split())
    separators = "=*_#-|–—―─━═﹣－·•"
    return len(value) >= 3 and all(character in separators for character in value)


def _cooldown_seconds(line: str) -> int | None:
    """Recognize the two intentional wait messages emitted by the pipeline."""
    if "cooldown" not in line.casefold():
        return None
    match = re.search(r"\b(?:retrying in|waiting)\s+(\d+)s\b", line, re.IGNORECASE)
    return max(1, int(match.group(1))) if match else None


def _masked_setting(key: str, value: Any) -> str:
    if _is_secret_setting_name(key):
        return "••••••••" if value else ""
    return str(value)


def _is_secret_setting_name(name: str) -> bool:
    upper = name.upper()
    return upper in _SECRET_NAMES or upper.endswith(_SECRET_SUFFIXES)


def _command_spec(
    value: Sequence[str] | Mapping[str, Any],
) -> tuple[list[str], str | None, dict[str, str]]:
    """Accept an injected command or an explicit command/run projection.

    The mapping form lets the application reveal the new run directory before
    Parse persists its first snapshot, while preserving a compact list form for
    tests and callers that already know the run.
    """
    if isinstance(value, Mapping):
        raw_command = value.get("command") or ()
        run_dir = value.get("run_dir")
        raw_environment = value.get("environment") or {}
        environment = (
            {str(name): str(item) for name, item in raw_environment.items()}
            if isinstance(raw_environment, Mapping)
            else {}
        )
        return (
            list(raw_command)
            if isinstance(raw_command, Sequence) and not isinstance(raw_command, str)
            else [],
            run_dir if isinstance(run_dir, str) else None,
            environment,
        )
    return list(value) if not isinstance(value, str) else [], None, {}


def _stylesheet(*, dark: bool, font_size: int = 13) -> str:
    background, text, surface, accent, border, blue = (
        ("#1b1f24", "#edf3ff", "#101722", "#1f2b3d", "#364152", "#7aa7e8") if dark
        else ("#f5f7fb", "#18202a", "#ffffff", "#eaf2ff", "#cbd5e1", "#2457a6")
    )
    muted = "#c9d6e8" if dark else "#526579"
    arrow_dir = (_LOCALE_DIR.parent).as_posix()
    progress_text = "#f3f7ff" if dark else "#18202a"
    progress_fill = "#315a91" if dark else "#a7cdf9"
    progress_error_text = "#ffe7e9" if dark else "#701523"
    progress_error_fill = "#843341" if dark else "#f4adb7"
    progress_warning_text = "#ffe2a3" if dark else "#714d00"
    progress_warning_fill = "#94702d" if dark else "#e6b645"
    warning_background = "#3a2b16" if dark else "#fff7df"
    warning_border = "#a77c2e" if dark else "#d5a437"
    progress_warning_background = "#332613" if dark else "#fff1c2"
    history_blue = "#7ab4ff" if dark else "#175fe6"
    history_primary = "#286fc4" if dark else history_blue
    history_primary_hover = "#347fd7" if dark else "#1455cf"
    history_disabled = "#202833" if dark else "#f1f3f7"
    return f"""
        QWidget {{ background: {background}; color: {text}; font-family: 'Segoe UI', sans-serif; font-size: {font_size}px; }}
        QLabel#settingsDescription {{ color: {muted}; background: transparent; }}
        QPlainTextEdit#commandsOutput {{ font-size: {max(15, font_size + 2)}px; }}
        #brandHeader, #card, #phaseLabel, #settingsSection {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; padding: 8px; }}
        #brandHeader QLabel, #settingsSection QWidget, #settingsSection QLabel {{ background: transparent; }}
        QLabel#settingHelpIcon {{ color: {blue}; background: {accent}; border: 1px solid {blue}; border-radius: 11px; font-size: 12px; font-weight: 700; padding: 0; }}
        QLabel#settingHelpIcon:hover {{ background: {'#294365' if dark else '#d6e7ff'}; border-color: {'#a8d1ff' if dark else '#174a8b'}; }}
        QFrame#brandLogoTile {{ background-color: #ffffff; border: 1px solid #d5dce8; border-radius: 8px; }}
        QFrame#brandLogoTile QLabel {{ background-color: #ffffff; }}
        QFrame#loadingSkeletonBar {{ background: transparent; border: 0; }}
        QFrame#libraryImportSkeletonBar {{ background: {'#40516b' if dark else '#d7e2f1'}; border: 0; border-radius: 4px; }}
        QWidget#libraryLoadingCell {{ background: transparent; }}
        #paperDropArea {{ border: 2px dashed {blue}; border-radius: 8px; min-height: 52px; padding: 6px; }}
        #llmStatus {{ background: {accent}; border: 1px solid {border}; border-radius: 7px; padding: 7px; }}
        #phaseLabel {{ font-weight: 600; }}
        #phaseLabel[error="true"] {{ background: {'#3b1d24' if dark else '#fff0f0'}; color: {'#ffb8bd' if dark else '#9f1828'}; border: 1px solid {'#d66a75' if dark else '#d93b4d'}; }}
        #phaseLabel[warning="true"] {{ background: {warning_background}; color: {progress_warning_text}; border: 1px solid {warning_border}; }}
        #currentActivity {{ color: {text}; background: transparent; padding: 2px 1px; min-height: 20px; }}
        #currentActivity[error="true"], #phaseProgressPercent[error="true"] {{ color: {'#ffb8bd' if dark else '#9f1828'}; font-weight: 600; }}
        #currentActivity[warning="true"], #phaseProgressPercent[warning="true"] {{ color: {progress_warning_text}; font-weight: 600; }}
        #phaseProgressPercent {{ color: {muted}; font-size: {max(11, font_size - 1)}px; font-weight: 600; }}
        #cooldownStatus {{ color: {'#c8ddff' if dark else '#153b76'}; background: {'#1c314d' if dark else '#edf4ff'}; border: 1px solid {'#5f90d0' if dark else '#9bbde9'}; border-radius: 8px; padding: 6px 8px; }}
        QTableWidget, QPlainTextEdit, QTextBrowser, QLineEdit, QComboBox, QListWidget, QTreeWidget, QSpinBox {{ background: {surface}; border: 1px solid {border}; border-radius: 7px; padding: 5px; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid {blue}; }}
        QLineEdit[unconfigured="true"], QComboBox[unconfigured="true"], QSpinBox[unconfigured="true"] {{ background: transparent; }}
        QComboBox::drop-down, QSpinBox::up-button, QSpinBox::down-button {{ border: 0; background: {accent}; width: 22px; border-radius: 5px; }}
        QComboBox::down-arrow {{ image: url({arrow_dir}/chevron-down.svg); width: 10px; height: 6px; }}
        QSpinBox::up-arrow {{ image: url({arrow_dir}/chevron-up.svg); width: 9px; height: 5px; }}
        QSpinBox::down-arrow {{ image: url({arrow_dir}/chevron-down.svg); width: 9px; height: 5px; }}
        QComboBox QAbstractItemView {{ background: {surface}; color: {text}; border: 1px solid {border}; selection-background-color: {blue}; selection-color: white; outline: 0; }}
        QMenu#libraryImportMenu {{ background: {surface}; color: {text}; border: 1px solid {border}; border-radius: 8px; padding: 6px; font-size: {max(14, font_size + 1)}px; }}
        QMenu#libraryImportMenu::item {{ padding: 8px 14px; margin: 2px 4px; min-width: 184px; min-height: 20px; border-radius: 5px; background: transparent; }}
        QMenu#libraryImportMenu::item:selected {{ background: {blue}; color: {'#101722' if dark else '#ffffff'}; }}
        QMenu#libraryImportMenu::item:disabled {{ color: #8795a8; }}
        QProgressBar#libraryImportProgressBar {{ background: {accent}; color: {text}; border: 1px solid {border}; border-radius: 7px; min-height: 22px; text-align: center; }}
        QProgressBar#libraryImportProgressBar::chunk {{ background: {'#315a91' if dark else '#a7cdf9'}; border-radius: 6px; }}
        QCheckBox {{ spacing: 8px; background: transparent; }}
        QCheckBox::indicator, QTableWidget#libraryReviewTable::indicator {{ width: 17px; height: 17px; border: 1px solid {border}; border-radius: 5px; background: {surface}; }}
        QCheckBox::indicator:hover, QTableWidget#libraryReviewTable::indicator:hover {{ border-color: {blue}; }}
        QCheckBox::indicator:checked, QTableWidget#libraryReviewTable::indicator:checked {{ background: {blue}; border-color: {blue}; image: url({arrow_dir}/checkmark.svg); }}
        QCheckBox::indicator:indeterminate, QTableWidget#libraryReviewTable::indicator:indeterminate {{ background: {blue}; border-color: {blue}; }}
        QTableWidget {{ gridline-color: {border}; alternate-background-color: {accent}; }}
        QHeaderView::section {{ background: {accent}; border: 0; border-bottom: 1px solid {border}; padding: 6px; font-weight: 600; }}
        QListWidget#navigationSidebar {{ padding: 8px; border-radius: 10px; outline: 0; show-decoration-selected: 1; }}
        QListWidget#navigationSidebar::item {{ padding: 10px 8px; margin: 2px; border-radius: 6px; border: 1px solid transparent; }}
        QListWidget#navigationSidebar::item:selected {{ background: {blue}; color: white; font-weight: 700; border-color: {blue}; outline: 0; }}
        QListWidget#settingsSearchResults {{ border: 1px solid {border}; border-radius: 8px; outline: 0; }}
        QListWidget#settingsSearchResults::item {{ padding: 6px 8px; border-bottom: 1px solid {border}; }}
        QListWidget#settingsSearchResults::item:selected {{ background: {blue}; color: white; }}
        QPushButton {{ background: {accent}; border: 1px solid {border}; border-radius: 7px; padding: 7px 10px; font-weight: 600; }}
        QPushButton:hover {{ border-color: {blue}; }} QPushButton:checked, #primaryButton {{ background: {blue}; color: white; border-color: {blue}; }}
        QSplitter::handle:horizontal {{ background: transparent; border-left: 1px solid {border}; width: 5px; }}
        QSplitter::handle:horizontal:hover {{ background: {accent}; border-left-color: {history_blue}; }}
        QPushButton#historyResumeButton, QPushButton#historyParseReviewButton, QPushButton#historyRegenerateReport, QPushButton#historyVerifyForkButton, QPushButton#historyReportFolder, QPushButton#historyExportReportButton {{ color: {history_blue}; background: {surface}; border: 1px solid {history_blue}; }}
        QPushButton#historyResumeButton:hover, QPushButton#historyParseReviewButton:hover, QPushButton#historyRegenerateReport:hover, QPushButton#historyVerifyForkButton:hover, QPushButton#historyReportFolder:hover, QPushButton#historyExportReportButton:hover {{ background: {accent}; border-color: {history_blue}; }}
        QPushButton#historyResumeButton:disabled, QPushButton#historyParseReviewButton:disabled, QPushButton#historyRegenerateReport:disabled, QPushButton#historyVerifyForkButton:disabled, QPushButton#historyReportFolder:disabled, QPushButton#historyExportReportButton:disabled {{ color: #8795a8; background: {history_disabled}; border-color: {border}; }}
        QPushButton#historyOpenReport[primaryAction="true"] {{ color: #ffffff; background: {history_primary}; border-color: {history_primary}; }}
        QPushButton#historyOpenReport[primaryAction="true"]:hover {{ color: #ffffff; background: {history_primary_hover}; border-color: {history_primary_hover}; }}
        QPushButton#historyOpenReport[primaryAction="true"]:disabled {{ color: #8795a8; background: {history_disabled}; border-color: {border}; }}
        QPushButton#juryModelsCheck[providerCheckState="success"] {{ color: {'#a5f3c5' if dark else '#14532d'}; background: {'#174d35' if dark else '#dcfce7'}; border-color: {'#63d98a' if dark else '#187343'}; }}
        QPushButton#juryModelsCheck[providerCheckState="error"] {{ color: {'#ffd0cc' if dark else '#8a1c13'}; background: {'#641d1d' if dark else '#fee4e2'}; border-color: {'#ff746c' if dark else '#b42318'}; }}
        QTreeWidget#llmChoices {{ alternate-background-color: {accent}; }}
        QTreeWidget#llmChoices::item {{ min-height: 28px; padding: 3px; }}
        QTreeWidget#llmChoices::item:hover {{ background: {accent}; }}
        QTreeWidget#llmChoices::item:selected {{ background: {blue}; color: white; }}
        #primaryButton:hover {{ background: {'#99bfff' if dark else '#1b4a93'}; border-color: {'#99bfff' if dark else '#1b4a93'}; color: white; }}
        #primaryButton:pressed {{ background: {'#5f90d0' if dark else '#153b76'}; border-color: {'#5f90d0' if dark else '#153b76'}; color: white; padding: 8px 10px 6px; }}
        #runStartButton {{ background: #16803a; color: white; border-color: #16803a; }}
        #runStartButton:hover {{ background: #126b31; border-color: #126b31; }}
        #runStopButton {{ background: #b52b36; color: white; border-color: #b52b36; }}
        #runStopButton:hover {{ background: #94222c; border-color: #94222c; }}
        #exportReportButton {{ background: {'#174b49' if dark else '#d7f2ee'}; color: {'#d8faf4' if dark else '#075d55'}; border-color: {'#3b9288' if dark else '#69b8ac'}; }}
        #exportReportButton:hover {{ background: {'#24645f' if dark else '#bce7df'}; border-color: {'#62bcae' if dark else '#298e80'}; }}
        #exportReportButton:disabled {{ background: {background}; color: #8795a8; border-color: {border}; }}
        #phaseProgress {{ min-height: 8px; max-height: 8px; border: 0; border-radius: 4px; background: {accent}; color: {progress_text}; text-align: center; font-size: {max(10, font_size - 2)}px; }}
        #phaseProgress::chunk {{ border-radius: 6px; background: {progress_fill}; }}
        #phaseProgress[error="true"] {{ background: {'#3b1d24' if dark else '#ffe5e8'}; color: {progress_error_text}; }}
        #phaseProgress[error="true"]::chunk {{ background: {progress_error_fill}; }}
        #phaseProgress[warning="true"] {{ background: {progress_warning_background}; color: {progress_warning_text}; }}
        #phaseProgress[warning="true"]::chunk {{ background: {progress_warning_fill}; }}
        QPushButton:focus, QToolButton:focus {{ border: 2px solid {blue}; outline: 0; }}
        QPushButton:disabled {{ color: #8795a8; background: {background}; }}
        QToolButton {{ background: {accent}; border: 1px solid {border}; border-radius: 7px; padding: 5px 7px; font-weight: 700; }}
        QToolButton:hover {{ border-color: {blue}; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 24px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
        QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 24px; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        #settingNote {{ color: #64748b; font-size: {max(11, font_size - 1)}px; background: transparent; }}
        #historyActions {{ border: 0; background: transparent; }}
        #sectionTitle {{ font-size: 16px; font-weight: 700; }}
        #settingsFooter {{ background: {surface}; border: 1px solid {border}; border-radius: 9px; }}
        QTreeWidget#documentationOutline {{ border: 0; border-right: 1px solid {border}; border-radius: 0; padding: 8px; background: {background}; font-size: {max(12, font_size)}px; }}
        QTreeWidget#documentationOutline::item {{ padding: 7px 8px; margin: 1px 0; border-radius: 6px; }}
        QTreeWidget#documentationOutline::item:hover {{ background: {accent}; }}
        QTreeWidget#documentationOutline::item:selected {{ background: {blue}; color: white; font-weight: 600; }}
    """


def _documentation_css(*, dark: bool, font_size: int) -> str:
    body, heading, code, code_border, link, quote = (
        ("#edf3ff", "#ffffff", "#111827", "#3b4759", "#8db8ff", "#c9d6e8") if dark
        else ("#18202a", "#0f2746", "#eef4fc", "#cbd5e1", "#2457a6", "#526579")
    )
    return f"""
        body {{ color: {body}; font-family: 'Segoe UI', sans-serif; font-size: {max(14, font_size + 1)}px; line-height: 1.55; }}
        p {{ margin: 0 0 12px; }}
        ul, ol {{ margin: 6px 0 14px 22px; }}
        li {{ margin: 4px 0; }}
        h1 {{ color: {heading}; font-size: {max(23, font_size + 9)}px; margin: 14px 0 10px; }}
        h2 {{ color: {heading}; font-size: {max(19, font_size + 6)}px; margin: 22px 0 8px; }}
        h3 {{ color: {heading}; font-size: {max(16, font_size + 3)}px; margin: 18px 0 6px; }}
        a {{ color: {link}; font-weight: 600; text-decoration: none; }}
        code {{ background: {code}; color: {body}; border: 1px solid {code_border}; border-radius: 4px; font-family: Consolas, monospace; padding: 2px 4px; }}
        pre {{ background: {code}; color: {body}; border: 1px solid {code_border}; border-radius: 8px; font-family: Consolas, monospace; padding: 12px; margin: 14px 0; white-space: pre-wrap; }}
        blockquote {{ color: {quote}; border-left: 3px solid {link}; padding-left: 10px; }}
    """
