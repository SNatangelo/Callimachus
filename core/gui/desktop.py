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
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from core.app.runtime_paths import (
    application_argv,
    is_frozen,
    resource_root,
    user_data_root,
)


_LOCALE_DIR = Path(__file__).with_name("assets") / "locales"
_STOP_ESCALATION_GRACE_MS = 10_000
_CONSOLE_PAGE_BYTES = 128 * 1024
_CONSOLE_LIVE_WINDOW_BYTES = 128 * 1024
_CONSOLE_EXPORT_CHUNK_BYTES = 64 * 1024
_LLM_ROLE_PREFERENCE_KEY = "llm_role_selection"
_SECRET_SUFFIXES = (
    "_API_KEY", "_API_TOKEN", "_AUTH_TOKEN", "_ACCESS_TOKEN",
    "_SECRET", "_PASSWORD", "_CREDENTIAL",
)
_SECRET_NAMES = {"CITATION_VERIFIER_SIGNING_KEY"}


def load_translations(language: str = "en") -> dict[str, str]:
    """Load a complete external catalogue, falling back to English."""
    candidate = _LOCALE_DIR / f"{language}.json"
    fallback = _LOCALE_DIR / "en.json"
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = json.loads(fallback.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in data.items()}


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


def _source_summary_html(row: Mapping[str, Any], text: Mapping[str, str], *, dark: bool) -> str:
    """Explain persisted source facts without exposing the raw snapshot payload."""
    details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
    parsed = details.get("parsed") if isinstance(details.get("parsed"), Mapping) else {}
    resolved = details.get("resolved") if isinstance(details.get("resolved"), Mapping) else {}
    fetched = details.get("fetch") if isinstance(details.get("fetch"), Mapping) else {}
    pairs = details.get("verification_pairs") if isinstance(details.get("verification_pairs"), list) else []

    def value(item: Any) -> str:
        return escape(str(item).strip()) if item not in (None, "") else escape(text["summary_not_recorded"])

    def line(label: str, item: Any) -> str:
        return f"<p><strong>{escape(label)}</strong> {value(item)}</p>"

    def section(title: str, body: str) -> str:
        return f"<div class='card'><h2>{escape(title)}</h2>{body}</div>"

    phase = str(row.get("phase") or "").casefold()
    phase_label = text.get(f"phase_{phase}", phase.title() or text["summary_not_recorded"])
    status = {
        "completato": text["status_complete"],
        "in_corso": text["status_running"],
        "richiede_intervento": text["status_action_required"],
        "in_attesa": text["status_waiting"],
    }.get(str(row.get("status") or "").casefold(), row.get("status"))
    identity = line(text["summary_title"], parsed.get("title") or parsed.get("raw_entry") or row.get("title"))
    identity += line(text["summary_identifier"], parsed.get("doi") or parsed.get("pmid") or parsed.get("isbn") or row.get("ref_id"))

    if resolved:
        lookup_status = str(resolved.get("status") or "").casefold()
        lookup_explanation = text.get(f"summary_lookup_{lookup_status}")
        resolution = f"<p>{escape(lookup_explanation)}</p>" if lookup_explanation else ""
        resolution += line(
            text["summary_resolution_status"],
            text.get(f"summary_lookup_label_{lookup_status}", resolved.get("status")),
        )
        if resolved.get("matched_title"):
            resolution += line(text["summary_match"], resolved["matched_title"])
        if resolved.get("via"):
            resolution += line(text["summary_method"], resolved["via"])
        if resolved.get("reason"):
            resolution += line(text["summary_reason"], resolved["reason"])
    else:
        resolution = f"<p>{escape(text['summary_resolution_pending'])}</p>"

    if fetched:
        tier = str(fetched.get("tier") or "").casefold()
        tasks = fetched.get("pending_tasks") or ()
        fetch_message_key = (
            f"summary_acquired_{tier}" if tier in {"fulltext", "abstract"}
            else "summary_awaiting_manual" if tasks
            else "summary_no_usable_text"
        )
        fetch = f"<p>{escape(text[fetch_message_key])}</p>"
        fetch += line(text["summary_text"], {
            "fulltext": text["fulltext"],
            "abstract": text["abstract"],
        }.get(tier, text["no_text"]))
        if isinstance(tasks, (list, tuple)) and tasks:
            fetch += line(text["summary_manual_tasks"], len(tasks))
        best_source = fetched.get("best_source")
        if isinstance(best_source, Mapping):
            source_label = next(
                (best_source.get(key) for key in ("source_ref", "url", "source_text_id", "path") if best_source.get(key)),
                None,
            )
            if source_label:
                fetch += line(text["summary_best_source"], source_label)
    else:
        fetch = f"<p>{escape(text['summary_fetch_pending'])}</p>"

    verdict_items = []
    for pair in pairs:
        if not isinstance(pair, Mapping):
            continue
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
        claim = pair.get("claim_id")
        cause = pair.get("terminal_cause")
        summary = f"{escape(text['summary_claim'])} {value(claim)}: {value(outcome)}" if claim else value(outcome)
        if cause:
            summary += f"<br><small>{escape(text['summary_decision_basis'])} {value(str(cause).replace('_', ' '))}</small>"
        verdict_items.append(f"<li>{summary}</li>")
    verification = (
        f"<ul>{''.join(verdict_items)}</ul>" if verdict_items
        else f"<p>{escape(text['summary_no_verdict'])}</p>"
    )
    foreground, muted, surface, border = (
        ("#edf3ff", "#c9d6e8", "#182333", "#40516a") if dark
        else ("#18202a", "#526579", "#f5f8fc", "#cbd5e1")
    )
    return f"""
        <html><head><style>
            body {{ color: {foreground}; font-family: 'Segoe UI', sans-serif; font-size: 14px; line-height: 1.45; }}
            h1 {{ font-size: 20px; margin: 0 0 10px; }}
            h2 {{ font-size: 15px; margin: 0 0 8px; }}
            p {{ margin: 0 0 7px; }}
            .intro {{ color: {muted}; margin-bottom: 16px; }}
            .card {{ background: {surface}; border: 1px solid {border}; padding: 12px; margin: 0 0 12px; }}
        </style></head><body>
        <h1>{value(row.get('title') or row.get('ref_id'))}</h1>
        <p class="intro">{escape(text['summary_progress'].format(phase=phase_label, status=status or text['summary_not_recorded']))}</p>
        {section(text['summary_identity'], identity)}
        {section(text['summary_resolution'], resolution)}
        {section(text['summary_fetch'], fetch)}
        {section(text['summary_verification'], verification)}
        </body></html>
    """


def create_desktop_window(
    *,
    language: str | None = None,
    command_builder: Callable[[str, str, str], Sequence[str] | Mapping[str, Any]] | None = None,
    resume_command_builder: Callable[[str], Sequence[str] | Mapping[str, Any]] | None = None,
    snapshot_loader: Callable[[str], Mapping[str, Any]] | None = None,
    overview_loader: Callable[[str], Mapping[str, Any]] | None = None,
    source_page_loader: Callable[[str, int, int], Mapping[str, Any]] | None = None,
    latest_loader: Callable[[], Mapping[str, Any] | None] | None = None,
    source_detail_loader: Callable[[str, str], Mapping[str, Any]] | None = None,
    history_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    history_brief_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    history_detail_loader: Callable[[str], Mapping[str, Any]] | None = None,
    cache_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    settings_loader: Callable[[], Mapping[str, Any]] | None = None,
    settings_saver: Callable[[Mapping[str, str]], None] | None = None,
    signing_key_generator: Callable[[], Mapping[str, Any]] | None = None,
    verify_preview_loader: Callable[[str], Mapping[str, Any]] | None = None,
    verify_candidates_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    verify_selection_preview_loader: Callable[[Mapping[str, list[str]], str], Mapping[str, Any]] | None = None,
    preference_loader: Callable[[], str | None] | None = None,
    preference_saver: Callable[[str], None] | None = None,
    docs_loader: Callable[[], Mapping[str, str] | str] | None = None,
    resume_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    report_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    export_report_callback: Callable[[str, str], Any] | None = None,
    skip_manual_callback: Callable[[str], int] | None = None,
    ocr_pending_count_loader: Callable[[str], int] | None = None,
    verify_fork_options_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    verify_fork_command_builder: Callable[[Mapping[str, Any], list[str]], Any] | None = None,
    verify_fork_selector: Callable[[Sequence[Mapping[str, Any]]], Sequence[str] | None] | None = None,
    delete_cache_callback: Callable[[Sequence[Mapping[str, Any]]], Any] | None = None,
    clear_cache_callback: Callable[[], Any] | None = None,
    delete_history_callback: Callable[[Sequence[Mapping[str, Any]]], Any] | None = None,
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
    verify_selection_preview_loader = verify_selection_preview_loader or (lambda _roles, _level: {})
    verify_fork_options_loader = verify_fork_options_loader or (lambda: ())
    docs_loader = docs_loader or (lambda: {"README.md": text["no_documentation"]})

    class LoadSignals(QtCore.QObject):
        completed = QtCore.Signal(str, int, object, object)

    class DesktopWindow(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self._run_dir: str | None = None
            self._background_loading = background_loading
            self._source_page_mode = background_loading and source_page_loader is not None
            self._load_tokens: dict[str, int] = {}
            self._load_futures: dict[str, Any] = {}
            self._loading_kinds: set[str] = set()
            self._load_signals = LoadSignals()
            self._load_signals.completed.connect(self._load_completed)
            self._load_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="callimachus-gui") if background_loading else None
            self._load_closed = False
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
            self._skeleton_effects: list[Any] = []
            self._run_environment_by_dir: dict[str, dict[str, str]] = {}
            self._process = None
            self._commands_process = None
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
            self._source_overview_signature = None
            self._source_page_stale = False
            self._pending_child_source_dir: str | None = None
            self._source_sort_column: int | None = None
            self._source_sort_ascending = True
            self._history_checked_run_dirs: set[str] = set()
            self._cache_checked_keys: set[str] = set()
            self._history_checkbox_pressed_state = None
            self._cache_checkbox_pressed_state = None
            self._dark = False
            self._spinner_frame = 0
            self._stopping = False
            self._new_analysis_reset_pending = False
            self._stop_escalation_timer = None
            self._forced_stop_process = None
            self._cooldown_until: float | None = None
            self._cooldown_activity = ""
            self._last_phase_progress: int | None = None
            self._language = language
            self._settings_rows: list[Mapping[str, Any]] = []
            self._settings_editors: dict[str, Any] = {}
            self._settings_initial: dict[str, str] = {}
            self._settings_dirty: dict[str, str] = {}
            self._settings_replacing: set[str] = set()
            self._secret_editors: dict[str, Any] = {}
            self._settings_loaded = False
            self._page_index = 0
            self._font_size = self._load_font_size()
            self.setWindowTitle(text["app_title"])
            self.resize(1360, 900)
            self.setMinimumSize(960, 640)
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
            self._refresh_static_tabs()

        def _build(self):
            central = QtWidgets.QWidget(self)
            layout = QtWidgets.QVBoxLayout(central)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(8)
            layout.addWidget(self._header(central))
            body = QtWidgets.QHBoxLayout()
            body.setContentsMargins(0, 0, 0, 0)
            self.navigation = QtWidgets.QListWidget(central)
            self.navigation.setObjectName("navigationSidebar")
            self.navigation.setFixedWidth(160)
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
            self.navigation.setCurrentRow(0)
            body.addWidget(self.navigation)
            body.addWidget(self.pages, 1)
            layout.addLayout(body, 1)
            self.setCentralWidget(central)
            self._apply_theme(False)

        def _header(self, parent):
            header = QtWidgets.QFrame(parent)
            header.setObjectName("brandHeader")
            row = QtWidgets.QHBoxLayout(header)
            row.setContentsMargins(10, 6, 10, 6)
            logo = QtWidgets.QLabel(header)
            logo.setObjectName("brandLogo")
            logo.setPixmap(_wordmark(QtCore, QtGui, 215, 46, self.devicePixelRatioF(), dark=False))
            logo.setFixedSize(215, 46)
            logo.setScaledContents(False)
            self.brand_logo = logo
            row.addWidget(logo)
            row.addStretch()
            self.theme_toggle = QtWidgets.QPushButton(text["theme_dark"], header)
            self.theme_toggle.setObjectName("themeToggle")
            self.theme_toggle.setCheckable(True)
            self.theme_toggle.toggled.connect(self._apply_theme)
            row.addWidget(self.theme_toggle)
            self.font_decrease = QtWidgets.QToolButton(header)
            self.font_decrease.setText("A−")
            self.font_decrease.setToolTip(text["font_decrease"])
            self.font_decrease.clicked.connect(lambda: self._change_font_size(-1))
            row.addWidget(self.font_decrease)
            self.font_increase = QtWidgets.QToolButton(header)
            self.font_increase.setText("A+")
            self.font_increase.setToolTip(text["font_increase"])
            self.font_increase.clicked.connect(lambda: self._change_font_size(1))
            row.addWidget(self.font_increase)
            return header

        def _analysis_tab(self):
            tab = QtWidgets.QWidget()
            outer = QtWidgets.QHBoxLayout(tab)
            outer.setContentsMargins(0, 4, 0, 0)
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
            self.mode_group = QtWidgets.QButtonGroup(self)
            self.mode_group.setExclusive(True)
            self.mode_buttons = {}
            for index, (key, value) in enumerate((("maximum", "mode_maximum"), ("abstract", "mode_abstract"),
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
                modes.addWidget(button, index // 2, index % 2)
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
            form.addWidget(QtWidgets.QLabel(text["llm_choices"], config))
            self.llm_choices = QtWidgets.QTableWidget(0, 3, config)
            self.llm_choices.setObjectName("llmChoices")
            self.llm_role_choices: dict[str, tuple[Any, Any]] = {}
            self._llm_checkbox_cells: dict[Any, Any] = {}
            self._normalizing_llm_roles = False
            self._llm_role_preferences_loaded = False
            self._llm_role_preferences: dict[str, set[str]] | None = None
            self._llm_role_preferences_invalid = False
            self.llm_choices.setHorizontalHeaderLabels((
                text["llm_model"], text["jury1_role"], text["jury2_role"],
            ))
            self.llm_choices.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.NoSelection
            )
            self.llm_choices.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            )
            self.llm_choices.verticalHeader().setVisible(False)
            header = self.llm_choices.horizontalHeader()
            header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
            for column in (1, 2):
                header.setSectionResizeMode(
                    column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
                )
            self.llm_choices.setMinimumHeight(88)
            self.llm_choices.setMaximumHeight(150)
            form.addWidget(self.llm_choices)
            self.llm_status = QtWidgets.QLabel(text["llm_unavailable"], config)
            self.llm_status.setObjectName("llmStatus")
            self.llm_status.setWordWrap(True)
            form.addWidget(self.llm_status)
            self.jury_models = QtWidgets.QLabel("", config)
            self.jury_models.setObjectName("juryModels")
            self.jury_models.setWordWrap(True)
            form.addWidget(self.jury_models)
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
            form.addWidget(self.phase_progress)
            self.current_activity = QtWidgets.QLabel(
                text["current_activity"].format(activity=text["activity_waiting"]), config
            )
            self.current_activity.setObjectName("currentActivity")
            self.current_activity.setWordWrap(True)
            form.addWidget(self.current_activity)
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
            left_scroll.setMinimumWidth(350)
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
            self.source_table.cellDoubleClicked.connect(self._show_source_details)
            self.source_table.cellClicked.connect(
                lambda row, column: (
                    self._show_source_details(row, column) if column == 8 else None
                )
            )
            self.source_stack = QtWidgets.QStackedWidget(tab)
            self.source_stack.addWidget(self.source_table)
            self.source_stack.addWidget(self._skeleton_widget(tab))
            right.addWidget(self.source_stack, 1)
            self.source_more_button = QtWidgets.QPushButton(text["show_more"], tab)
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
            self._refresh_llm_choices()
            self._refresh_llm_status()
            self._update_run_controls()
            return tab

        def _history_tab(self):
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
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
            layout.addWidget(self.history_stack, 1)
            self.history_more_button = QtWidgets.QPushButton(text["show_more"], tab)
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
            report.clicked.connect(self._regenerate_report)
            self.history_report_button = report
            self.history_report_button.setEnabled(False)
            open_report = QtWidgets.QPushButton(text["open_report"], tab)
            open_report.clicked.connect(self._open_selected_history_report)
            self.history_open_report_button = open_report
            self.history_open_report_button.setEnabled(False)
            export_report = QtWidgets.QPushButton(text["export_report"], tab)
            export_report.setObjectName("exportReportButton")
            export_report.setToolTip(text["export_report_tip"])
            export_report.clicked.connect(self._export_selected_history_report)
            self.history_export_report_button = export_report
            export_report.setEnabled(False)
            verify_fork = QtWidgets.QPushButton(text["rerun_verify"], tab)
            verify_fork.clicked.connect(self._fork_selected_history_verify)
            self.history_verify_fork_button = verify_fork
            self.history_verify_fork_button.setEnabled(False)
            delete = QtWidgets.QPushButton(text["delete_selected_runs"], tab)
            delete.clicked.connect(self._delete_selected_history)
            self.history_delete_button = delete
            self.history_delete_button.setEnabled(False)
            actions.addWidget(delete, 0, 1)
            actions.setColumnStretch(2, 1)
            actions.addWidget(resume, 1, 0)
            actions.addWidget(parse_review, 1, 1)
            actions.addWidget(report, 1, 2)
            actions.addWidget(verify_fork, 1, 3)
            actions.addWidget(open_report, 1, 4)
            actions.addWidget(export_report, 1, 5)
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
            self.cache_summary = QtWidgets.QLabel("", tab)
            self.cache_summary.setObjectName("cacheSummary")
            self.cache_summary.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            layout.addWidget(self.cache_summary)
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
            self.cache_table.cellPressed.connect(self._remember_cache_checkbox_state)
            self.cache_table.cellClicked.connect(self._toggle_cache_checkbox_cell)
            self.cache_stack = QtWidgets.QStackedWidget(tab)
            self.cache_stack.addWidget(self.cache_table)
            self.cache_stack.addWidget(self._skeleton_widget(tab))
            layout.addWidget(self.cache_stack, 1)
            self.cache_more_button = QtWidgets.QPushButton(text["show_more"], tab)
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

        def _settings_tab(self):
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(tab)
            self.settings_categories = QtWidgets.QListWidget(tab)
            self.settings_categories.setObjectName("settingsCategories")
            self.settings_categories.setMinimumWidth(165)
            self.settings_categories.setMaximumWidth(210)
            self.settings_stack = QtWidgets.QStackedWidget(tab)
            self.settings_categories.currentRowChanged.connect(self._select_settings_category)
            layout.addWidget(self.settings_categories)
            settings_panel = QtWidgets.QWidget(tab)
            panel_layout = QtWidgets.QVBoxLayout(settings_panel)
            panel_layout.setContentsMargins(0, 0, 0, 0)
            scroll = QtWidgets.QScrollArea(settings_panel)
            self.settings_scroll = scroll
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.settings_content = QtWidgets.QWidget(scroll)
            self.settings_layout = QtWidgets.QVBoxLayout(self.settings_content)
            self.settings_layout.setSizeConstraint(
                QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize
            )
            self.settings_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            scroll.setWidget(self.settings_content)
            panel_layout.addWidget(scroll, 1)
            footer = QtWidgets.QFrame(settings_panel)
            footer.setObjectName("settingsFooter")
            footer_layout = QtWidgets.QHBoxLayout(footer)
            footer_layout.setContentsMargins(8, 8, 8, 8)
            footer_layout.addStretch()
            self.settings_discard_button = QtWidgets.QPushButton(text["discard_changes"], footer)
            self.settings_discard_button.clicked.connect(self._discard_settings)
            self.settings_save_button = QtWidgets.QPushButton(text["save_changes"], footer)
            self.settings_save_button.setObjectName("primaryButton")
            self.settings_save_button.clicked.connect(self._save_settings)
            footer_layout.addWidget(self.settings_discard_button)
            footer_layout.addWidget(self.settings_save_button)
            panel_layout.addWidget(footer)
            self.settings_stack.addWidget(settings_panel)
            self.settings_stack.addWidget(self._skeleton_widget(tab))
            layout.addWidget(self.settings_stack, 1)
            return tab

        def _info_tab(self):
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            title = QtWidgets.QLabel(text["info_title"], tab)
            title.setObjectName("sectionTitle")
            layout.addWidget(title)
            content = QtWidgets.QHBoxLayout()
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
            layout.addLayout(content, 1)
            return tab

        def _commands_tab(self):
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            layout.setContentsMargins(8, 4, 8, 8)
            title = QtWidgets.QLabel(text["commands_title"], tab)
            title.setObjectName("sectionTitle")
            layout.addWidget(title)

            self.commands_expert_notice = QtWidgets.QLabel(
                text["commands_expert_notice"], tab
            )
            self.commands_expert_notice.setObjectName("commandsExpertNotice")
            self.commands_expert_notice.setWordWrap(True)
            layout.addWidget(self.commands_expert_notice)

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
            console_tools.addWidget(self.commands_export_button)
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
            widget = QtWidgets.QWidget(parent)
            layout = QtWidgets.QVBoxLayout(widget)
            layout.setContentsMargins(18, 18, 18, 18)
            layout.setSpacing(13)
            for index in range(7):
                bar = QtWidgets.QFrame(widget)
                bar.setObjectName("loadingSkeletonBar")
                bar.setFixedHeight(20 if index % 3 == 0 else 14)
                bar.setMaximumWidth(560 if index % 3 == 0 else 350 + index * 18)
                effect = QtWidgets.QGraphicsOpacityEffect(bar)
                effect.setOpacity(0.6)
                bar.setGraphicsEffect(effect)
                self._skeleton_effects.append(effect)
                layout.addWidget(bar)
            layout.addStretch()
            return widget

        @staticmethod
        def _page_batch_size(table):
            row_height = max(20, table.verticalHeader().defaultSectionSize())
            visible_rows = max(1, table.viewport().height() // row_height)
            return max(12, min(80, visible_rows * 2))

        def _reset_source_page(self, *_args):
            self._source_visible_count = self._page_batch_size(self.source_table)
            if self._source_page_mode:
                self._request_source_query()
            else:
                self._render_sources()

        def _reset_cache_page(self, *_args):
            self._cache_visible_count = self._page_batch_size(self.cache_table)
            self._render_cache()

        def _show_more_sources(self):
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
            )

        def _request_source_page(self):
            if (
                not self._source_page_mode or not self._run_dir
                or self._page_index != 0 or self._source_query_active()
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

        def _refresh_source_pages(self):
            if (
                not self._source_page_mode or not self._run_dir
                or self._page_index != 0 or self._source_query_active()
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
            if not self._source_page_mode:
                return
            self._source_query_timer.stop()
            self._invalidate_load("source_query")
            if not self._source_query_active():
                self._source_query_signature = None
                self._source_rows = list(self._source_paged_rows)
                self._source_visible_count = max(
                    self._page_batch_size(self.source_table), len(self._source_rows)
                )
                self._render_sources()
                if self._source_page_loaded_run == self._run_dir:
                    self.source_stack.setCurrentIndex(0)
                    if self._source_page_stale:
                        self._refresh_source_pages()
                else:
                    self._request_source_page()
                return
            if self._run_dir and self._page_index == 0:
                self.source_stack.setCurrentIndex(1)
                self._source_query_timer.start(250)

        def _load_source_query(self):
            if not self._run_dir or self._page_index != 0 or not self._source_query_active():
                return
            run_dir = self._run_dir
            signature = (
                self.source_search.text(), self.source_filter.currentText(),
                self.source_phase_filter.currentData(), self._source_sort_column,
                self._source_sort_ascending,
            )
            self._source_query_signature = signature
            self._queue_load(
                "source_query",
                lambda: (run_dir, signature, snapshot_loader(run_dir)),
            )

        def _show_more_history(self):
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
            self._cache_visible_count += self._page_batch_size(self.cache_table)
            self._render_cache()

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

        def _invalidate_load(self, kind: str):
            self._load_tokens[kind] = self._load_tokens.get(kind, 0) + 1
            self._loading_kinds.discard(kind)
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
                        self.source_stack.setCurrentIndex(0)
                        if self._source_page_stale:
                            self._refresh_source_pages()
                    else:
                        self._request_source_page()
                if not self._run_dir or (
                    self._snapshot_loaded_run == self._run_dir
                    and self._process is None
                    and self._pending_finish is None
                ):
                    self.source_stack.setCurrentIndex(0)
                else:
                    self._request_snapshot(force=True)
            elif index == 1:
                self.history_stack.setCurrentIndex(1)
                if history_brief_loader is not None and history_detail_loader is not None:
                    self._queue_load("history_brief", history_brief_loader)
                else:
                    self._queue_load("history", history_loader)
            elif index == 2:
                self.cache_stack.setCurrentIndex(1)
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
                self._source_query_timer.stop()
                self._invalidate_load("source_page")
                self._invalidate_load("source_refresh")
                self._invalidate_load("source_query")
                if not self._latest_loaded:
                    self._invalidate_load("latest")
                if self._source_page_mode and self._source_query_active():
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
                self._invalidate_load("history_brief")
                self._invalidate_load("history_details")
                self._history_rows = []
                self._history_visible_count = 0
                self._history_requested_dirs.clear()
                self.history_table.setRowCount(0)
                self.history_more_button.hide()
            elif index == 2:
                self._cache_rows = []
                self._cache_visible_count = 0
                self.cache_table.setRowCount(0)
                self.cache_more_button.hide()
            elif index == 3:
                self._settings_loaded = False
            elif index == 5:
                self._documents.clear()
                self.docs_outline.clear()
                self.docs_view.clear()

        def _load_completed(self, kind: str, token: int, payload: Any, error: Any):
            if self._load_closed or token != self._load_tokens.get(kind):
                return
            self._loading_kinds.discard(kind)
            self._load_futures.pop(kind, None)
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
                            source_counts,
                        )
                        if (
                            self._source_overview_signature is not None
                            and signature != self._source_overview_signature
                        ):
                            self._source_page_stale = True
                            if self._source_query_active() and self._page_index == 0:
                                self._request_source_query()
                            else:
                                self._refresh_source_pages()
                        self._source_overview_signature = signature
                        if self._page_index == 0 and not self._source_query_active():
                            if self._source_page_loaded_run != run_dir:
                                self._request_source_page()
                            elif (
                                isinstance(counts, Mapping)
                                and isinstance(counts.get("sources_total"), int)
                                and counts["sources_total"] != self._source_paged_total
                            ):
                                self._source_page_stale = True
                                self._refresh_source_pages()
                    if not self._source_page_mode:
                        self.source_stack.setCurrentIndex(0)
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
                self._source_next_offset = len(self._source_paged_rows)
                self._source_paged_total = total
                self._source_page_stale = signature != self._source_overview_signature
                if not self._source_query_active() and self._page_index == 0:
                    self._source_rows = list(self._source_paged_rows)
                    self._source_visible_count = max(
                        self._page_batch_size(self.source_table), len(self._source_rows)
                    )
                    self._render_sources()
                    self.source_stack.setCurrentIndex(0)
                if self._source_page_stale:
                    self._refresh_source_pages()
                return
            if kind == "source_page":
                self.source_more_button.setEnabled(True)
                run_dir, offset, page = payload if isinstance(payload, tuple) else (None, None, None)
                if run_dir != self._run_dir or error:
                    if error:
                        self._show_load_error(kind, str(error))
                    return
                if not isinstance(page, Mapping) or offset != self._source_next_offset:
                    self._show_load_error(kind, "invalid source page")
                    return
                references = page.get("references") or []
                total = page.get("total")
                if not isinstance(total, int) or total < 0 or not isinstance(references, list):
                    self._show_load_error(kind, "invalid source page")
                    return
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
                    self.source_stack.setCurrentIndex(0)
                    if self._source_page_stale:
                        self._refresh_source_pages()
                return
            if kind == "source_query":
                run_dir, signature, snapshot = payload if isinstance(payload, tuple) else (None, None, None)
                if error:
                    self._show_load_error(kind, str(error))
                    return
                if run_dir != self._run_dir or signature != self._source_query_signature:
                    return
                if not isinstance(snapshot, Mapping):
                    self._show_load_error(kind, "invalid source search")
                    return
                self._source_rows = list(snapshot.get("references") or ())
                self._source_visible_count = self._page_batch_size(self.source_table)
                self._render_sources()
                self.source_stack.setCurrentIndex(0)
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
            if kind == "history_brief" and self._page_index == 1:
                self._apply_history_rows(payload)
                self.history_stack.setCurrentIndex(0)
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
                self.history_stack.setCurrentIndex(0)
            elif kind == "cache" and self._page_index == 2:
                self._apply_cache_rows(payload)
                self.cache_stack.setCurrentIndex(0)
            elif kind == "settings" and self._page_index == 3:
                self._apply_settings_payload(payload or {})
                self.settings_stack.setCurrentIndex(0)
            elif kind == "docs" and self._page_index == 5:
                self._render_documentation(payload or {"README.md": text["no_documentation"]})
                self.docs_stack.setCurrentIndex(0)
            elif kind == "detail" and self._detail_dialog is not None:
                _dialog, view, row, stack = self._detail_dialog
                details = payload if isinstance(payload, Mapping) else {}
                view.setHtml(_source_summary_html({**row, "details": details}, text, dark=self._dark))
                stack.setCurrentIndex(0)

        def _show_load_error(self, kind: str, reason: str):
            message = text["load_failed"].format(reason=reason)
            if kind == "snapshot":
                self.phase_label.setText(message)
                self._set_error_state(True)
                self.source_stack.setCurrentIndex(0)
            elif kind in {"source_page", "source_query", "source_refresh"}:
                self.source_table.setRowCount(1)
                self.source_table.setItem(0, 1, QtWidgets.QTableWidgetItem(message))
                self.source_more_button.hide()
                self.source_stack.setCurrentIndex(0)
            elif kind in {"history", "history_brief", "history_details"}:
                self.history_table.setRowCount(1)
                self.history_table.setItem(0, 1, QtWidgets.QTableWidgetItem(message))
                self.history_stack.setCurrentIndex(0)
            elif kind == "cache":
                self.cache_table.setRowCount(1)
                self.cache_table.setItem(0, 1, QtWidgets.QTableWidgetItem(message))
                self.cache_stack.setCurrentIndex(0)
            elif kind == "docs":
                self.docs_view.setPlainText(message)
                self.docs_stack.setCurrentIndex(0)
            elif kind == "detail" and self._detail_dialog is not None:
                self._detail_dialog[1].setPlainText(message)
                self._detail_dialog[3].setCurrentIndex(0)
            elif kind == "settings":
                self.phase_label.setText(message)
                self.settings_stack.setCurrentIndex(0)

        def _request_snapshot(self, *, force: bool = False):
            if not self._background_loading or not self._run_dir:
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

        def _apply_theme(self, dark: bool):
            self._dark = bool(dark)
            self.theme_toggle.setText(text["theme_light"] if self._dark else text["theme_dark"])
            self.setStyleSheet(_stylesheet(dark=self._dark, font_size=self._font_size))
            if self._parse_review_window is not None and hasattr(
                self._parse_review_window, "set_dark_theme"
            ):
                self._parse_review_window.set_dark_theme(self._dark)
            console_font = self.commands_output.font()
            console_font.setPixelSize(max(15, self._font_size + 2))
            self.commands_output.setFont(console_font)
            self.brand_logo.setPixmap(_wordmark(
                QtCore, QtGui, 215, 46, self.devicePixelRatioF(), dark=self._dark
            ))
            if hasattr(self, "start_button"):
                self._balance_run_button_geometry()
            for combo in self.findChildren(QtWidgets.QComboBox):
                self._style_combo_popup(combo)
            if hasattr(self, "docs_view"):
                css = _documentation_css(dark=self._dark, font_size=self._font_size)
                self.docs_view.document().setDefaultStyleSheet(css)
                current = getattr(self, "_current_doc_path", "")
                if current and current in getattr(self, "_documents", {}):
                    self.docs_view.setMarkdown(self._documents[current])
                    self._format_documentation_document()
            if hasattr(self, "history_actions_widget"):
                self.history_actions_widget.ensurePolished()
                self.history_actions_widget.layout().activate()
                self.history_actions_widget.adjustSize()
                self.history_actions_scroll.setMinimumHeight(
                    self.history_actions_widget.sizeHint().height() + 12
                )
                self.history_actions_scroll.setMaximumHeight(
                    self.history_actions_widget.sizeHint().height() + 12
                )

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

        def _load_font_size(self) -> int:
            try:
                value = int(QtCore.QSettings("Callimachus", "Callimachus").value("font_size", 13))
            except (TypeError, ValueError):
                value = 13
            return max(11, min(18, value))

        def _change_font_size(self, delta: int):
            self._font_size = max(11, min(18, self._font_size + delta))
            try:
                QtCore.QSettings("Callimachus", "Callimachus").setValue("font_size", self._font_size)
            except Exception:
                pass
            self._apply_theme(self._dark)

        def eventFilter(self, watched, event):  # noqa: N802
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
                if len(event.mimeData().urls()) == 1 and event.mimeData().urls()[0].isLocalFile():
                    event.acceptProposedAction()
                    return True
            if watched is self.paper_drop and event.type() == QtCore.QEvent.Type.Drop:
                urls = event.mimeData().urls()
                if len(urls) == 1 and urls[0].isLocalFile():
                    self._set_paper(urls[0].toLocalFile())
                    event.acceptProposedAction()
                    return True
            return super().eventFilter(watched, event)

        def _browse_paper(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                text["select_paper"],
                filter=(
                    f"{text['manuscripts']} (*.pdf *.docx *.tex *.md *.txt *.html);;"
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
            if (
                self._new_analysis_reset_pending
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
            if self._process is not None:
                return False

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
                for kind in ("snapshot", "source_page", "source_refresh", "source_query"):
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
            self.current_activity.setText(
                text["current_activity"].format(activity=text["activity_waiting"])
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

        def _start_analysis(self):
            if self._commands_process is not None or self._new_analysis_reset_pending:
                return
            paper = getattr(self, "_paper_path", "")
            command, run_dir, environment = _command_spec(
                command_builder(paper, self._selected_mode(), self._selected_jury_level())
            )
            if self.skip_parse_review.isChecked():
                command = [item for item in command if item != "--manual-review"]
            elif "--manual-review" not in command:
                command.append("--manual-review")
            selected_preview = self._selected_llm_preview()
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
            self._start_process(command, environment=environment)

        def _start_process(
            self,
            command: Sequence[str],
            *,
            environment: Mapping[str, str] | None = None,
            auto_open_report: bool = True,
        ):
            if (
                self._process is not None
                or self._commands_process is not None
                or self._new_analysis_reset_pending
            ):
                return
            command = self._command_with_manual_policy(command)
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
            for name, value in (environment or {}).items():
                process_environment.insert(str(name), str(value))
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
            for effect in self._skeleton_effects:
                effect.setOpacity(opacity)
            if self._console_page_loading:
                self.console_history_spinner.setText(
                    "◐◓◑◒"[self._spinner_frame % 4]
                )
            self._update_cooldown_status()
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
            payload = stdout + stderr
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
            stdout_decoder = self._analysis_stdout_decoder
            stderr_decoder = self._analysis_stderr_decoder
            stdout_text = stdout_decoder.decode(stdout, final=final) if stdout_decoder else ""
            stderr_text = stderr_decoder.decode(stderr, final=final) if stderr_decoder else ""
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
            self._cancel_stop_escalation()
            self._forced_stop_process = None
            self._process = None
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
            if forced_stop:
                self._set_error_state(True)
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
                    self._update_run_controls()
                    return
                if self._parse_review_available:
                    self._open_parse_review(self._run_dir)
                elif self.skip_manual_checks.isChecked():
                    if self._skip_pending_manual_tasks(self._run_dir):
                        self._resume_after_manual_skip(self._run_dir)
                elif self._guided_fetch_available:
                    self._open_guided_fetch(self._run_dir)
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
            self._update_run_controls()

        def _set_error_state(self, enabled: bool):
            for widget in (self.phase_label, self.phase_progress):
                widget.setProperty("error", enabled)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()
            if enabled and self.phase_progress.maximum() == 0:
                self.phase_progress.setRange(0, 100)
                self.phase_progress.setValue(self._last_phase_progress or 0)

        def _refresh_snapshot(self):
            if not self._run_dir:
                return None
            try:
                snapshot = dict(snapshot_loader(self._run_dir) or {})
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
                return None
            return self._apply_snapshot(snapshot)

        def _apply_snapshot(self, snapshot: Mapping[str, Any]):
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
            if isinstance(progress, int):
                self._last_phase_progress = max(0, min(100, progress))
                self.phase_progress.setRange(0, 100)
                self.phase_progress.setValue(self._last_phase_progress)
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
            )
            self.skip_manual_checks.setEnabled(
                not active and not review_open and not commands_active and not reset_pending
            )
            self.skip_parse_review.setEnabled(
                not active and not review_open and not commands_active and not reset_pending
            )
            commands_button = getattr(self, "commands_run_button", None)
            if commands_button is not None:
                commands_button.setEnabled(
                    not active and not review_open and not commands_active and not reset_pending
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
            )
            self.start_button.setText("" if active else text["start"])
            self.start_button.setIcon(_spinner_icon(QtCore, QtGui, self._spinner_frame) if active else QtGui.QIcon())
            if active:
                self.resume_latest_button.setObjectName("runStopButton")
                self.resume_latest_button.setText(text["stopping"] if self._stopping else text["stop"])
                self.resume_latest_button.setEnabled(not self._stopping)
            else:
                self.resume_latest_button.setObjectName("runResumeButton")
                self.resume_latest_button.setText(text["resume_latest"])
                row, tooltip = self._latest_resume_state()
                self.resume_latest_button.setToolTip(tooltip)
                self.resume_latest_button.setEnabled(
                    not review_open and not commands_active and not reset_pending
                    and row is not None
                )
            self.resume_latest_button.style().unpolish(self.resume_latest_button)
            self.resume_latest_button.style().polish(self.resume_latest_button)
            self._balance_run_button_geometry()
            self.open_guided_fetch_button.setVisible(
                bool(
                    self._run_dir and self._guided_fetch_available
                    and not active and not review_open and not commands_active
                )
            )
            self.open_parse_review_button.setVisible(
                bool(
                    self._run_dir and self._parse_review_available
                    and not active and not commands_active
                    and self._guided_window is None
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
            self.open_run_report_button.setVisible(report_visible)
            self.export_run_report_button.setVisible(report_visible)
            self.export_run_report_button.setEnabled(
                not active and not review_open and not commands_active
                and self._current_run_exportable()
            )

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

        def _export_report_for_run(self, run_dir: str):
            if export_report_callback is None:
                return
            default_path = str(Path(run_dir).parent / f"{Path(run_dir).name}.public.html")
            output, _filter = QtWidgets.QFileDialog.getSaveFileName(
                self, text["export_report"], default_path, "HTML (*.html)"
            )
            if not output:
                return
            try:
                path = export_report_callback(run_dir, output)
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
                QtWidgets.QMessageBox.warning(
                    self, text["export_report"],
                    text["export_report_failed"].format(reason=exc),
                )
                return
            QtWidgets.QMessageBox.information(
                self, text["export_report"],
                text["export_report_done"].format(path=path),
            )

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
                return False
            return True

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
                return
            if command:
                self._start_process(
                    command,
                    environment={**self._run_environment_by_dir.get(run_dir, {}), **environment},
                )

        def _balance_run_button_geometry(self):
            buttons = (self.start_button, self.resume_latest_button)
            for button in buttons:
                button.setMinimumSize(0, 0)
                button.ensurePolished()
            width = max(button.sizeHint().width() for button in buttons)
            height = max(button.sizeHint().height() for button in buttons)
            for button in buttons:
                button.setMinimumWidth(width)
                button.setMinimumHeight(height)
                button.updateGeometry()

        def _resume_latest_or_stop(self):
            if self._commands_process is not None:
                return
            if self._process is not None:
                self._request_process_stop()
                return
            row, _tooltip = self._latest_resume_state()
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
            self._stopping = True
            process.write(b"STOP\n")
            self._schedule_stop_escalation(process)
            self._update_run_controls()
            return True

        def _resume_run_row(self, row: Mapping[str, Any]):
            if self._commands_process is not None or self._new_analysis_reset_pending:
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
            environment = {
                **self._run_environment_by_dir.get(target_dir, {}),
                **environment,
            }
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
            if target_dir == run_dir and self._guided_fetch_available and not self._skip_pending_manual_tasks(run_dir):
                return
            self._start_process(command, environment=environment)

        def _refresh_llm_choices(self):
            existing_states = {
                selector: (jury1.isChecked(), jury2.isChecked())
                for selector, (jury1, jury2) in self.llm_role_choices.items()
            }
            if not self._llm_role_preferences_loaded:
                self._llm_role_preferences = self._load_llm_role_preferences()
                self._llm_role_preferences_loaded = True
            self.llm_choices.setRowCount(0)
            self.llm_role_choices: dict[str, tuple[Any, Any]] = {}
            self._llm_checkbox_cells = {}
            options = list(verify_candidates_loader() or ())
            for option in options:
                selector = str(option.get("selector") or "")
                if not selector:
                    continue
                label = str(option.get("label") or selector)
                row = self.llm_choices.rowCount()
                self.llm_choices.insertRow(row)
                item = QtWidgets.QTableWidgetItem(label)
                item.setToolTip(label)
                self.llm_choices.setItem(row, 0, item)

                initial_both = bool(option.get("configured")) or len(options) == 1
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
                        selector, checked
                    )
                )
                jury2 = QtWidgets.QCheckBox("", self.llm_choices)
                jury2.setChecked(jury2_selected)
                jury2.setAccessibleName(f"{selector}: {text['jury2_role']}")
                jury2.setAccessibleDescription(text["jury2_lane_tip"])
                jury2.setToolTip(text["jury2_lane_tip"])
                jury2.toggled.connect(
                    lambda checked, selector=selector: self._on_llm_role_toggled(
                        selector, checked
                    )
                )
                self.llm_choices.setCellWidget(row, 1, self._llm_checkbox_cell(jury1))
                self.llm_choices.setCellWidget(row, 2, self._llm_checkbox_cell(jury2))
                self.llm_role_choices[selector] = (jury1, jury2)
            self._normalize_single_active_model()

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
            active = [
                selector
                for selector, (jury1, jury2) in self.llm_role_choices.items()
                if jury1.isChecked() or jury2.isChecked()
            ]
            if len(active) != 1:
                return
            jury1, jury2 = self.llm_role_choices[active[0]]
            self._normalizing_llm_roles = True
            try:
                jury1.setChecked(True)
                jury2.setChecked(True)
            finally:
                self._normalizing_llm_roles = False

        def _on_llm_role_toggled(self, selector: str, checked: bool):
            if self._normalizing_llm_roles:
                return
            active = [
                name
                for name, (jury1, jury2) in self.llm_role_choices.items()
                if jury1.isChecked() or jury2.isChecked()
            ]
            normalize_to: tuple[str, bool, bool] | None = None
            if checked and len(active) == 1:
                normalize_to = (selector, True, True)
            elif not checked and len(active) == 1:
                remaining = active[0]
                normalize_to = (
                    (selector, False, False)
                    if remaining == selector
                    else (remaining, True, True)
                )
            if normalize_to is not None:
                target, jury1_checked, jury2_checked = normalize_to
                jury1, jury2 = self.llm_role_choices[target]
                self._normalizing_llm_roles = True
                try:
                    jury1.setChecked(jury1_checked)
                    jury2.setChecked(jury2_checked)
                finally:
                    self._normalizing_llm_roles = False
            saved = self._save_llm_role_preferences()
            self._refresh_llm_status()
            if not saved:
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
                return dict(verify_selection_preview_loader(
                    self._selected_llm_roles(), self._selected_jury_level()
                ) or {})
            except (OSError, ValueError):
                return {}

        def _refresh_llm_status(self, *_args):
            preview = self._selected_llm_preview()
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
            jury2_active = preview.get("jury2_level") != "off"
            for button in self.jury_buttons:
                button.setEnabled(available and can_change and jury2_active)
            jury1 = ", ".join(str(item) for item in preview.get("jury1") or ())
            jury2 = ", ".join(str(item) for item in preview.get("jury2") or ())
            if available and preview.get("jury2_level") == "off":
                jury2 = text["jury2_inactive"]
            self.jury_models.setText(
                f"{text['jury1_role']}: {jury1 or text['unavailable']}\n"
                f"{text['jury2_role']}: {jury2 or text['unavailable']}"
            )

        def set_run_dir(self, run_dir: str):
            """Attach an existing run for display or history-driven resume."""
            self._display_run_done = False
            self._display_references_only = None
            if self._background_loading:
                self._invalidate_load("snapshot")
                self._snapshot_loading_run = None
                self._snapshot_loaded_run = None
                self._invalidate_load("source_page")
                self._invalidate_load("source_refresh")
                self._invalidate_load("source_query")
                self._source_query_timer.stop()
                self._source_paged_rows = []
                self._source_paged_total = 0
                self._source_next_offset = 0
                self._source_page_loaded_run = None
                self._source_overview_signature = None
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
                        1: row.get("title") or row.get("source") or row.get("ref_id") or text["unavailable"],
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
                else len(rows) > self._source_visible_count
            )
            self.source_more_button.setVisible(more_available)
            self.source_table.setRowCount(min(len(rows), self._source_visible_count))
            for index, (row, source_number) in enumerate(rows[:self._source_visible_count]):
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
                status = {
                    "completato": f"✓ {text['status_complete']}",
                    "in_corso": (
                        f"{'◐◓◑◒'[self._spinner_frame % 4]} {text['status_running']}"
                    ),
                    "richiede_intervento": f"! {text['status_action_required']}",
                    "in_attesa": f"○ {text['status_waiting']}",
                }.get(raw_status.casefold(), raw_status or text["unavailable"])
                values = (source_number, row.get("title") or row.get("source") or row.get("ref_id") or text["unavailable"], row.get("phase") or text["unavailable"], status, availability, row.get("risk_signal") or "", row.get("review_labels") or "", row.get("result") or row.get("verdict") or text["unavailable"], text["details"])
                for column, value in enumerate(values):
                    item = QtWidgets.QTableWidgetItem(str(value))
                    if column == 0:
                        item.setData(QtCore.Qt.ItemDataRole.UserRole, dict(row))
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
                        item.setBackground(QtGui.QColor(color))
                        item.setForeground(QtGui.QColor("#18202a"))
                    if column == 5 and value:
                        item.setForeground(QtGui.QColor("#b42318"))
                        item.setToolTip(str(row.get("risk_tooltip") or ""))
                    if column == 6 and value:
                        item.setForeground(QtGui.QColor("#b15c00"))
                        item.setToolTip(str(row.get("review_tooltip") or ""))
                    self.source_table.setItem(index, column, item)

        def _show_source_details(self, row: int, _column: int):
            item = self.source_table.item(row, 0)
            payload = (
                item.data(QtCore.Qt.ItemDataRole.UserRole)
                if item is not None
                else None
            )
            if not isinstance(payload, Mapping):
                return
            if self._background_loading and source_detail_loader is not None and self._run_dir:
                if self._detail_dialog is not None:
                    self._detail_dialog[0].raise_()
                    return
                dialog = QtWidgets.QDialog(self)
                dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
                dialog.setWindowTitle(str(payload.get("title") or text["details"]))
                dialog.resize(760, 540)
                layout = QtWidgets.QVBoxLayout(dialog)
                stack = QtWidgets.QStackedWidget(dialog)
                view = QtWidgets.QTextBrowser(dialog)
                skeleton = self._skeleton_widget(dialog)
                stack.addWidget(view)
                stack.addWidget(skeleton)
                stack.setCurrentIndex(1)
                layout.addWidget(stack)
                close = QtWidgets.QPushButton("OK", dialog)
                close.clicked.connect(dialog.accept)
                layout.addWidget(close, 0, QtCore.Qt.AlignmentFlag.AlignRight)
                self._detail_dialog = (dialog, view, dict(payload), stack)
                effects = [bar.graphicsEffect() for bar in skeleton.findChildren(QtWidgets.QFrame)]

                def closed():
                    self._invalidate_load("detail")
                    self._skeleton_effects = [effect for effect in self._skeleton_effects if effect not in effects]
                    view.clear()
                    self._detail_dialog = None

                dialog.finished.connect(closed)
                dialog.open()
                run_dir, ref_id = self._run_dir, str(payload.get("ref_id") or "")
                self._queue_load("detail", lambda: source_detail_loader(run_dir, ref_id))
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(str(payload.get("title") or text["details"]))
            dialog.resize(760, 540)
            layout = QtWidgets.QVBoxLayout(dialog)
            view = QtWidgets.QTextBrowser(dialog)
            view.setOpenExternalLinks(False)
            view.setHtml(_source_summary_html(payload, text, dark=self._dark))
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
            self.history_more_button.setVisible(len(self._history_rows) > self._history_visible_count)
            self.history_table.setRowCount(min(len(self._history_rows), self._history_visible_count))
            for index, row in enumerate(self._history_rows[:self._history_visible_count]):
                counts = row.get("source_counts") or {}
                verification = row.get("verification_counts") or {}
                outcomes = verification.get("outcomes") or {}
                verdicts = [
                    f"{name} {count}" for name, count in sorted(outcomes.items())
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
                if isinstance(settings_payload, Mapping):
                    settings = [{"group": "advanced", "label": key, "name": key,
                                 "control": "secret" if _is_secret_setting_name(str(key)) else "text",
                                 "configured": bool(value), "value": "" if _is_secret_setting_name(str(key)) else value,
                                 "source": ""}
                                for key, value in sorted(settings_payload.items())]
                else:
                    settings = list(settings_payload)
                self._render_settings(settings)

        def _request_page(self, index: int):
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
            if index == 0:
                self._refresh_llm_choices()
                self._refresh_llm_status()
                if self._background_loading:
                    self._request_page_data(index)
            else:
                self._refresh_static_tabs()

        def _settings_category(self, row: Mapping[str, Any]) -> str:
            group = str(row.get("group") or "advanced").lower()
            if group == "models":
                return "models"
            return {
                "general": "general", "verify": "verify", "resolution": "resolution",
                "fetch": "fetch", "integrity": "integrity", "advanced": "advanced",
            }.get(group, "advanced")

        def _render_settings(self, settings):
            self._settings_rows = [row for row in settings if isinstance(row, Mapping)]
            self._settings_initial = {
                str(row.get("name") or ""): str(row.get("value") or "")
                for row in self._settings_rows if row.get("name")
            }
            self._settings_dirty.clear()
            self._settings_replacing.clear()
            self._settings_loaded = True
            categories = ("general", "verify", "resolution", "fetch", "integrity", "advanced", "models")
            current = self.settings_categories.currentRow()
            self.settings_categories.blockSignals(True)
            self.settings_categories.clear()
            for category in categories:
                if category in {"general", "integrity"} or any(self._settings_category(row) == category for row in self._settings_rows):
                    self.settings_categories.addItem(text.get("group_" + category, category.title()))
            self.settings_categories.blockSignals(False)
            self.settings_categories.setCurrentRow(max(0, min(current, self.settings_categories.count() - 1)))
            self._select_settings_category(self.settings_categories.currentRow())

        def _select_settings_category(self, index: int):
            if index < 0:
                return
            self._render_settings_category(self._settings_categories_key(index))

        def _settings_categories_key(self, index: int) -> str:
            item = self.settings_categories.item(index)
            if item is None:
                return "advanced"
            reverse = {text.get("group_" + key, key.title()): key for key in
                       ("general", "verify", "resolution", "fetch", "integrity", "advanced", "models")}
            return reverse.get(item.text(), "advanced")

        def _render_settings_category(self, category: str):
            while self.settings_layout.count():
                child = self.settings_layout.takeAt(0)
                if child.widget() is not None:
                    child.widget().hide()
                    child.widget().deleteLater()
                elif child.layout() is not None:
                    self._clear_layout(child.layout())
            self._settings_editors = {}
            self._secret_editors = {}
            if category == "general":
                card = QtWidgets.QFrame(self.settings_content); card.setObjectName("settingsSection")
                form = QtWidgets.QFormLayout(card)
                form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
                form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                form.addRow(QtWidgets.QLabel(text["application_language"], card), self._language_selector(card))
                self.settings_layout.addWidget(card)
            rows = [row for row in self._settings_rows if self._settings_category(row) == category]
            providers: dict[str, list[Mapping[str, Any]]] = {}
            for row in rows:
                providers.setdefault(str(row.get("provider") or ""), []).append(row)
            for provider, provider_rows in providers.items():
                card = QtWidgets.QFrame(self.settings_content); card.setObjectName("settingsSection")
                form = QtWidgets.QFormLayout(card)
                form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
                form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                if category == "models" and provider:
                    heading = QtWidgets.QLabel(provider, card); heading.setObjectName("sectionTitle")
                    form.addRow(heading, QtWidgets.QLabel("", card))
                for row in provider_rows:
                    self._add_setting_editor(form, card, row)
                self.settings_layout.addWidget(card)
            if category == "integrity":
                signing = QtWidgets.QFrame(self.settings_content); signing.setObjectName("settingsSection")
                signing_layout = QtWidgets.QVBoxLayout(signing)
                description = QtWidgets.QLabel(text["signing_key_description"], signing)
                description.setWordWrap(True)
                signing_layout.addWidget(description)
                signing_actions = QtWidgets.QHBoxLayout()
                generate = QtWidgets.QPushButton(text["generate_signing_key"], signing)
                generate.clicked.connect(self._generate_signing_key)
                signing_actions.addWidget(generate)
                self.signing_key_status = QtWidgets.QLabel("", signing)
                self.signing_key_status.setObjectName("settingNote")
                self.signing_key_status.setWordWrap(True)
                signing_actions.addWidget(self.signing_key_status, 1)
                signing_layout.addLayout(signing_actions)
                self.settings_layout.addWidget(signing)
            if category != "general" and not rows:
                empty = QtWidgets.QLabel(text["settings_empty"], self.settings_content)
                empty.setWordWrap(True); self.settings_layout.addWidget(empty)
            self.settings_layout.addStretch()

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

        def _add_setting_editor(self, form, parent, row: Mapping[str, Any]):
            name = str(row.get("name") or "")
            if not name:
                return
            label = QtWidgets.QLabel(str(row.get("label") or name), parent)
            label.setToolTip(str(row.get("description") or ""))
            label.setWordWrap(True)
            field = QtWidgets.QWidget(parent); layout = QtWidgets.QVBoxLayout(field); layout.setContentsMargins(0, 0, 0, 0)
            control = str(row.get("control") or "text")
            secret = control == "secret"
            configured = bool(row.get("configured"))
            value = self._settings_dirty.get(name, str(row.get("value") or ""))
            if control in {"boolean", "enum"}:
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
            description = str(row.get("description") or "")
            note = QtWidgets.QLabel(" · ".join(item for item in (description, status) if item), field)
            note.setObjectName("settingNote"); note.setWordWrap(True); layout.addWidget(note)
            self._settings_editors[name] = editor
            form.addRow(label, field)

        def _setting_status(self, row: Mapping[str, Any]) -> str:
            if row.get("configured"):
                state = text["setting_configured"]
            elif row.get("explicitly_empty"):
                state = text["setting_empty"]
            else:
                state = text["setting_unconfigured"]
            source = str(row.get("source") or "")
            source = text.get("setting_source_" + source, source)
            return f"{state}{' · ' + source if source else ''}"

        def _begin_secret_replacement(self, name, editor):
            if name not in self._settings_replacing:
                self._settings_replacing.add(name)
                editor.setReadOnly(False)
                editor.clear()
            editor.setFocus()

        def _stage_secret(self, name: str, value: str):
            # An empty field means "do not replace"; the existing secret remains intact.
            if value:
                self._settings_dirty[name] = value
            else:
                self._settings_dirty.pop(name, None)

        def _restore_empty_secret(self, name, editor):
            if name in self._settings_replacing and not editor.text() and name not in self._settings_dirty:
                self._settings_replacing.discard(name)
                editor.setReadOnly(True)
                editor.setText("••••••••")

        def _generate_signing_key(self):
            staged = dict(self._settings_dirty)
            replacing = set(self._settings_replacing)
            try:
                result = signing_key_generator() or {}
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, text["tab_settings"], str(exc))
                return
            path = str(result.get("path") or "")
            created = bool(result.get("created"))
            status = text["signing_key_created"] if created else text["signing_key_exists"]
            message = f"{status}{': ' + path if path else ''}"
            if hasattr(self, "signing_key_status"):
                self.signing_key_status.setText(message)
            # The generator persists only the key path.  Reload it immediately
            # so the active Integrity panel reflects the durable configuration.
            self._settings_loaded = False
            self._refresh_static_tabs()
            self._settings_dirty.update(staged)
            self._settings_replacing.update(replacing)
            integrity = next(
                (index for index in range(self.settings_categories.count())
                 if self._settings_categories_key(index) == "integrity"),
                -1,
            )
            if integrity >= 0:
                self.settings_categories.setCurrentRow(integrity)
                self._render_settings_category("integrity")
            if hasattr(self, "signing_key_status"):
                self.signing_key_status.setText(message)

        def _stage_setting(self, name: str, value: str):
            if value == self._settings_initial.get(name, ""):
                self._settings_dirty.pop(name, None)
            else:
                self._settings_dirty[name] = value

        def _save_settings(self) -> bool:
            if not self._settings_dirty:
                return True
            updates = dict(self._settings_dirty)
            try:
                if updates: settings_saver(updates)
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, text["tab_settings"], str(exc)); return False
            self._settings_dirty.clear(); self._settings_replacing.clear(); self._settings_loaded = False
            self._refresh_static_tabs(); return True

        def _discard_settings(self):
            self._settings_dirty.clear(); self._settings_replacing.clear(); self._settings_loaded = False
            self._refresh_static_tabs()

        def _confirm_settings_changes(self) -> str:
            prompt = QtWidgets.QMessageBox(self); prompt.setWindowTitle(text["tab_settings"])
            prompt.setText(text["unsaved_settings"])
            save = prompt.addButton(text["save_changes"], QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            discard = prompt.addButton(text["discard_changes"], QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            prompt.addButton(QtWidgets.QMessageBox.StandardButton.Cancel); prompt.exec()
            return "save" if prompt.clickedButton() is save else "discard" if prompt.clickedButton() is discard else "cancel"

        def _language_selector(self, parent):
            selector = QtWidgets.QComboBox(parent)
            selector.addItem("English", "en")
            selector.addItem("Italiano", "it")
            selector.setCurrentIndex(max(0, selector.findData(self._language)))
            self._style_combo_popup(selector)
            selector.currentIndexChanged.connect(lambda _index: preference_saver(str(selector.currentData())))
            selector.setToolTip(text["language_restart"])
            return selector

        def _render_documentation(self, documents):
            if isinstance(documents, str):
                documents = {"README.md": documents}
            self._documents = {str(path): str(body) for path, body in dict(documents).items()}
            self.docs_outline.blockSignals(True)
            self.docs_outline.clear()
            for path, markdown in self._documents.items():
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

        def _navigate_documentation(self, item, _previous=None):
            if item is None:
                return
            path, anchor = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ("", "")
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
                source = posixpath.normpath(posixpath.join("docs/guide", current))
                destination = posixpath.normpath(
                    posixpath.join(posixpath.dirname(source), target)
                )
                target = posixpath.relpath(destination, "docs/guide")
            if target in self._documents:
                for index in range(self.docs_outline.topLevelItemCount()):
                    item = self.docs_outline.topLevelItem(index)
                    if item.data(0, QtCore.Qt.ItemDataRole.UserRole)[0] == target:
                        self.docs_outline.setCurrentItem(item)
                        if anchor:
                            cursor = self.docs_view.document().find(anchor.replace("-", " "))
                            if not cursor.isNull(): self.docs_view.setTextCursor(cursor); self.docs_view.ensureCursorVisible()
                        return

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
                if query in " ".join(str(value) for value in row.values()).casefold()
                and (
                    not selected_key
                    or str(row.get("tier") or row.get("availability") or "").lower()
                    == selected_key
                )
            ]
            self._cache_filtered_rows = rows
            if self._cache_visible_count <= 0:
                self._cache_visible_count = self._page_batch_size(self.cache_table)
            self.cache_more_button.setVisible(len(rows) > self._cache_visible_count)
            self.cache_table.blockSignals(True)
            self.cache_table.setRowCount(min(len(rows), self._cache_visible_count))
            for index, row in enumerate(rows[:self._cache_visible_count]):
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
                for column, value in enumerate((row.get("title") or row.get("paper") or row.get("ref_id") or text["unavailable"], availability, row.get("updated_at") or row.get("updated") or text["unavailable"]), 1):
                    item = QtWidgets.QTableWidgetItem(str(value))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, dict(row))
                    if column == 2 and raw_availability == "abstract":
                        item.setToolTip(text["abstract_retry_tip"])
                    self.cache_table.setItem(index, column, item)
            self.cache_table.blockSignals(False)
            self._sync_cache_select_all()

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
                if item is not None:
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
            self.cache_delete_button.setEnabled(bool(self._checked_cache_rows()))

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
            protected = (
                run_dir == str(self._run_dir or "")
                and (self._process is not None or self._guided_window is not None)
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
                row.get("available") is True
                and row.get("run_status") == "completed"
                and row.get("phase") == "done"
                and row.get("references_only") is False
                and row.get("report_html_present") is True
                and isinstance(path, str)
                and Path(path).name == "report.html"
                and str(row.get("run_dir") or "")
            )

        def _update_history_actions(self):
            checked_rows = self._checked_history_rows()
            row = self._single_history_action_row()
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
                and self._parse_review_window is None
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
            self.history_export_report_button.setEnabled(
                export_report_callback is not None
                and isinstance(row, Mapping)
                and self._history_row_is_exportable(row)
            )
            self.history_verify_fork_button.setEnabled(
                isinstance(row, Mapping)
                and self._history_row_is_verify_forkable(row)
                and self._process is None
                and self._commands_process is None
                and self._parse_review_window is None
            )
            deletable = [
                candidate for candidate in checked_rows
                if self._history_row_is_deletable(candidate)
            ]
            self.history_delete_button.setEnabled(bool(checked_rows) and len(deletable) == len(checked_rows))

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

        def _export_selected_history_report(self):
            row = self._single_history_action_row()
            if isinstance(row, Mapping) and self._history_row_is_exportable(row):
                self._export_report_for_run(str(row["run_dir"]))

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
            row = self._single_history_action_row()
            if not isinstance(row, Mapping):
                return
            run_dir = row.get("run_dir")
            if not isinstance(run_dir, str):
                return
            try:
                current_rows = list(history_loader() or ())
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

        def _select_verify_fork_backends(
            self, options: Sequence[Mapping[str, Any]]
        ) -> list[str] | None:
            if verify_fork_selector is not None:
                selected = verify_fork_selector(options)
                return None if selected is None else [str(item) for item in selected]
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(text["rerun_verify"])
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.addWidget(QtWidgets.QLabel(text["select_verify_backends"], dialog))
            choices = QtWidgets.QListWidget(dialog)
            for option in options:
                item = QtWidgets.QListWidgetItem(
                    str(option.get("label") or option.get("backend") or "")
                )
                item.setData(
                    QtCore.Qt.ItemDataRole.UserRole,
                    str(option.get("selector") or ""),
                )
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.CheckState.Checked)
                choices.addItem(item)
            pressed_choice = [None, None]

            def remember_choice(item):
                pressed_choice[:] = [item, item.checkState()]

            def toggle_choice_row(item):
                if pressed_choice == [item, item.checkState()]:
                    item.setCheckState(
                        QtCore.Qt.CheckState.Unchecked
                        if item.checkState() == QtCore.Qt.CheckState.Checked
                        else QtCore.Qt.CheckState.Checked
                    )
                pressed_choice[:] = [None, None]

            choices.itemPressed.connect(remember_choice)
            choices.itemClicked.connect(toggle_choice_row)
            layout.addWidget(choices)
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Ok
                | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
                parent=dialog,
            )
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return None
            return [
                str(choices.item(index).data(QtCore.Qt.ItemDataRole.UserRole))
                for index in range(choices.count())
                if choices.item(index).checkState() == QtCore.Qt.CheckState.Checked
            ]

        def _fork_selected_history_verify(self):
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
            if not options:
                QtWidgets.QMessageBox.warning(
                    self, text["tab_history"], text["verify_backends_unavailable"]
                )
                return
            selected_lanes = self._select_verify_fork_backends(options)
            if not selected_lanes:
                return
            if verify_fork_command_builder is None:
                return
            try:
                result = verify_fork_command_builder(
                    current_row, selected_lanes
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
                self._start_process(command, environment=environment)

        def _delete_selected_cache(self):
            rows = self._checked_cache_rows()
            confirmed = rows and QtWidgets.QMessageBox.question(
                self,
                text["tab_cache"],
                text.get(
                    "confirm_delete_cache",
                    "Eliminare le voci selezionate dalla cache riutilizzabile?",
                ),
            ) == QtWidgets.QMessageBox.StandardButton.Yes
            if confirmed and delete_cache_callback is not None:
                try:
                    delete_cache_callback(rows)
                except (OSError, RuntimeError, ValueError) as exc:
                    QtWidgets.QMessageBox.warning(
                        self, text["tab_cache"], str(exc)
                    )
                else:
                    self._refresh_static_tabs()

        def _delete_selected_history(self):
            rows = self._checked_history_rows()
            if not rows or any(
                not self._history_row_is_deletable(row)
                for row in rows
            ):
                return
            confirmed = QtWidgets.QMessageBox.question(
                self, text["tab_history"], text["confirm_delete_runs"],
            ) == QtWidgets.QMessageBox.StandardButton.Yes
            if not confirmed:
                return
            try:
                delete_history_callback(rows)
            except (OSError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, text["tab_history"], str(exc))
            else:
                self._history_checked_run_dirs.clear()
                self._refresh_static_tabs()

        def _clear_cache(self):
            confirmed = QtWidgets.QMessageBox.question(
                self,
                text["tab_cache"],
                text.get(
                    "confirm_clear_cache",
                    "Svuotare tutta la cache riutilizzabile?",
                ),
            ) == QtWidgets.QMessageBox.StandardButton.Yes
            if confirmed and clear_cache_callback is not None:
                try:
                    clear_cache_callback()
                except (OSError, RuntimeError, ValueError) as exc:
                    QtWidgets.QMessageBox.warning(
                        self, text["tab_cache"], str(exc)
                    )
                else:
                    self._refresh_static_tabs()

        def _open_parse_review(self, run_dir: str):
            if self._process is not None or self._commands_process is not None:
                return
            if self._parse_review_window is not None:
                if self._parse_review_run_dir != run_dir:
                    return
                if self._parse_review_window.isHidden():
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
            if hasattr(self._parse_review_window, "set_dark_theme"):
                self._parse_review_window.set_dark_theme(self._dark)
            self._parse_review_window.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False
            )
            self._parse_review_window.show()
            self.phase_label.setText(text["parse_review_waiting"])
            self._update_run_controls()
            self._update_history_actions()

        def _continue_parse_review(self, run_dir: str):
            command, _ignored, environment = _command_spec(
                resume_command_builder(run_dir)
            )
            if not command:
                raise ValueError(text["resume_latest_no_history"])
            environment = {
                **self._run_environment_by_dir.get(run_dir, {}),
                **environment,
            }
            window = (
                self._parse_review_window
                if self._parse_review_run_dir == run_dir else None
            )
            if window is not None:
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

            def proceed():
                controller.proceed()
                self._guided_window = None
                command, _ignored_run_dir, environment = _command_spec(
                    resume_command_builder(run_dir)
                )
                if command:
                    self._start_process(
                        command,
                        environment={
                            **self._run_environment_by_dir.get(run_dir, {}),
                            **environment,
                        },
                    )

            def on_ocr_event(event):
                ref_id = event.get("ref_id") if isinstance(event, dict) else None
                status = event.get("status") if isinstance(event, dict) else None
                if not isinstance(ref_id, str) or status not in {"queued", "running", "done", "failed"}:
                    return
                reason = str(event.get("reason") or "").strip()
                suffix = f": {reason}" if reason else ""
                self._append_console_output(f"OCR [{ref_id}] {status}{suffix}\n")

            self._guided_window = factory(
                run_dir,
                submit_source=controller.stage_source,
                proceed=proceed,
                discard_source=controller.discard_source,
                run_ocr=getattr(controller, "run_ocr", None),
                on_ocr_event=on_ocr_event,
                ocr_text=text,
                submit_identity_review=controller.submit_identity_decision,
                identity_review_allowed=controller.identity_review_allowed,
            )
            self._guided_window.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True
            )
            def guided_closed():
                self._guided_window = None
                self._update_run_controls()
            self._guided_window.destroyed.connect(guided_closed)
            self._guided_window.show()
            self.phase_label.setText(text["guided_fetch_waiting"])
            self._update_run_controls()

        def closeEvent(self, event):
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
            if self._background_loading and self._load_pool is not None:
                self._load_closed = True
                for kind in tuple(self._load_futures):
                    self._invalidate_load(kind)
                self._load_pool.shutdown(wait=False, cancel_futures=True)
            self._cleanup_console_capture()
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


def _spinner_icon(QtCore, QtGui, frame: int):
    """Render a compact indeterminate spinner without a new asset dependency."""
    size = 24
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    pen = QtGui.QPen(QtGui.QColor("#ffffff"))
    pen.setWidth(3)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawArc(QtCore.QRectF(3, 3, 18, 18), -(frame % 12) * 30 * 16, 250 * 16)
    painter.end()
    return QtGui.QIcon(pixmap)


def _safe_activity_text(payload: bytes, limit: int = 280) -> str:
    """Keep child output diagnostic-only and avoid displaying credentials."""
    raw = payload.decode("utf-8", errors="replace")
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return ""
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", lines[-1])
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
    arrow_dir = (_LOCALE_DIR.parent).as_posix()
    progress_text = "#f3f7ff" if dark else "#18202a"
    progress_fill = "#315a91" if dark else "#a7cdf9"
    progress_error_text = "#ffe7e9" if dark else "#701523"
    progress_error_fill = "#843341" if dark else "#f4adb7"
    return f"""
        QWidget {{ background: {background}; color: {text}; font-family: 'Segoe UI', sans-serif; font-size: {font_size}px; }}
        QPlainTextEdit#commandsOutput {{ font-size: {max(15, font_size + 2)}px; }}
        #brandHeader, #card, #phaseLabel, #settingsSection {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; padding: 8px; }}
        #brandHeader QLabel, #settingsSection QWidget, #settingsSection QLabel {{ background: transparent; }}
        QFrame#brandLogoTile {{ background-color: #ffffff; border: 1px solid #d5dce8; border-radius: 8px; }}
        QFrame#brandLogoTile QLabel {{ background-color: #ffffff; }}
        QFrame#loadingSkeletonBar {{ background-color: {'#40516b' if dark else '#d7e2f1'}; border: 0; border-radius: 7px; }}
        #paperDropArea {{ border: 2px dashed {blue}; border-radius: 8px; min-height: 52px; padding: 6px; }}
        #llmStatus {{ background: {accent}; border: 1px solid {border}; border-radius: 7px; padding: 7px; }}
        #phaseLabel {{ font-weight: 600; }}
        #phaseLabel[error="true"] {{ background: {'#3b1d24' if dark else '#fff0f0'}; color: {'#ffb8bd' if dark else '#9f1828'}; border: 1px solid {'#d66a75' if dark else '#d93b4d'}; }}
        #currentActivity {{ color: {text}; background: transparent; padding: 2px 1px; min-height: 20px; }}
        #cooldownStatus {{ color: {'#c8ddff' if dark else '#153b76'}; background: {'#1c314d' if dark else '#edf4ff'}; border: 1px solid {'#5f90d0' if dark else '#9bbde9'}; border-radius: 8px; padding: 6px 8px; }}
        QTableWidget, QPlainTextEdit, QTextBrowser, QLineEdit, QComboBox, QListWidget, QTreeWidget, QSpinBox {{ background: {surface}; border: 1px solid {border}; border-radius: 7px; padding: 5px; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid {blue}; }}
        QLineEdit[unconfigured="true"], QComboBox[unconfigured="true"], QSpinBox[unconfigured="true"] {{ background: transparent; }}
        QComboBox::drop-down, QSpinBox::up-button, QSpinBox::down-button {{ border: 0; background: {accent}; width: 22px; border-radius: 5px; }}
        QComboBox::down-arrow {{ image: url({arrow_dir}/chevron-down.svg); width: 10px; height: 6px; }}
        QSpinBox::up-arrow {{ image: url({arrow_dir}/chevron-up.svg); width: 9px; height: 5px; }}
        QSpinBox::down-arrow {{ image: url({arrow_dir}/chevron-down.svg); width: 9px; height: 5px; }}
        QComboBox QAbstractItemView {{ background: {surface}; color: {text}; border: 1px solid {border}; selection-background-color: {blue}; selection-color: white; outline: 0; }}
        QCheckBox {{ spacing: 8px; background: transparent; }}
        QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid {border}; border-radius: 5px; background: {surface}; }}
        QCheckBox::indicator:hover {{ border-color: {blue}; }}
        QCheckBox::indicator:checked {{ background: {blue}; border-color: {blue}; image: url({arrow_dir}/checkmark.svg); }}
        QCheckBox::indicator:indeterminate {{ background: {blue}; border-color: {blue}; }}
        QTableWidget {{ gridline-color: {border}; alternate-background-color: {accent}; }}
        QHeaderView::section {{ background: {accent}; border: 0; border-bottom: 1px solid {border}; padding: 6px; font-weight: 600; }}
        QListWidget#navigationSidebar {{ padding: 8px; border-radius: 10px; outline: 0; show-decoration-selected: 1; }}
        QListWidget#navigationSidebar::item {{ padding: 10px 8px; margin: 2px; border-radius: 6px; border: 1px solid transparent; }}
        QListWidget#navigationSidebar::item:selected {{ background: {blue}; color: white; font-weight: 700; border-color: {blue}; outline: 0; }}
        QPushButton {{ background: {accent}; border: 1px solid {border}; border-radius: 7px; padding: 7px 10px; font-weight: 600; }}
        QPushButton:hover {{ border-color: {blue}; }} QPushButton:checked, #primaryButton {{ background: {blue}; color: white; border-color: {blue}; }}
        #primaryButton:hover {{ background: {'#99bfff' if dark else '#1b4a93'}; border-color: {'#99bfff' if dark else '#1b4a93'}; color: white; }}
        #primaryButton:pressed {{ background: {'#5f90d0' if dark else '#153b76'}; border-color: {'#5f90d0' if dark else '#153b76'}; color: white; padding: 8px 10px 6px; }}
        #runStartButton {{ background: #16803a; color: white; border-color: #16803a; }}
        #runStartButton:hover {{ background: #126b31; border-color: #126b31; }}
        #runResumeButton {{ background: #F59E0B; color: #241600; border-color: #F59E0B; }}
        #runResumeButton:hover {{ background: #D97706; border-color: #D97706; color: #241600; }}
        #runResumeButton:disabled {{ color: #725615; background: #f6dfa8; border-color: #ead39c; }}
        #runStopButton {{ background: #b52b36; color: white; border-color: #b52b36; }}
        #runStopButton:hover {{ background: #94222c; border-color: #94222c; }}
        #exportReportButton {{ background: {'#174b49' if dark else '#d7f2ee'}; color: {'#d8faf4' if dark else '#075d55'}; border-color: {'#3b9288' if dark else '#69b8ac'}; }}
        #exportReportButton:hover {{ background: {'#24645f' if dark else '#bce7df'}; border-color: {'#62bcae' if dark else '#298e80'}; }}
        #exportReportButton:disabled {{ background: {background}; color: #8795a8; border-color: {border}; }}
        #phaseProgress {{ min-height: 18px; max-height: 18px; border: 0; border-radius: 8px; background: {accent}; color: {progress_text}; text-align: center; font-size: {max(10, font_size - 2)}px; }}
        #phaseProgress::chunk {{ border-radius: 6px; background: {progress_fill}; }}
        #phaseProgress[error="true"] {{ background: {'#3b1d24' if dark else '#ffe5e8'}; color: {progress_error_text}; }}
        #phaseProgress[error="true"]::chunk {{ background: {progress_error_fill}; }}
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
