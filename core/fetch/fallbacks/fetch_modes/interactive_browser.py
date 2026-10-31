#!/usr/bin/env python3
# core/fetch/fallbacks/fetch_modes/interactive_browser.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Headed Playwright recovery for publisher challenges.

This is deliberately a human-in-the-loop fetch mode.  It never attempts to
solve a challenge: it opens an isolated visible browser, waits for the user to
complete it, and records only the response exposed by that same browser
session.  Callers must still send the recorded artifact through their normal
identity/content gates.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

from core.fetch import institutional_access
from core.fetch.extraction import fetch_html
from core.fetch.fallbacks.fetch_modes import browser_challenge

NAME = "interactive_browser"
# ``interactive`` remains an alias for runs created while this mode was being
# introduced.  The explicit name makes its deliberately narrow scope visible
# in the startup summary.
MODE_NAME = "interactive_challenge"
ALIASES = ("interactive", "playwright", "interactive_browser")


class CaptureToolbarStatus(Enum):
    """Readiness state of the optional Guided Fetch capture toolbar."""

    READY = "ready"
    PENDING = "pending"


ENV_INTERACTIVE_TIMEOUT = "CITATION_VERIFIER_FETCH_INTERACTIVE_TIMEOUT"
ENV_INTERACTIVE_POLL_SECONDS = "CITATION_VERIFIER_FETCH_INTERACTIVE_POLL_SECONDS"
DEFAULT_INTERACTIVE_TIMEOUT = 300
DEFAULT_INTERACTIVE_POLL_SECONDS = 2.0
_PDF_VIEWER_TARGETS_SCRIPT = r"""() => {
  const targets = [];
  const visible = (element) => {
    const rect = element.getBoundingClientRect();
    if (!(rect.width > 0 && rect.height > 0)) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  };
  const add = (value, kind, type = '') => {
    if (!value) return;
    try {
      const url = new URL(value, document.baseURI);
      const pdfType = /application\/pdf/i.test(type);
      const pdfPath = /\.pdf$/i.test(url.pathname);
      if (pdfType || pdfPath) {
        targets.push({url: url.href, kind, type});
      }
    } catch (_) { /* invalid viewer URL */ }
  };
  const scan = (root) => {
    for (const element of root.querySelectorAll('*')) {
      const tag = element.tagName.toLowerCase();
      if (visible(element)) {
        if (tag === 'object') {
          add(element.getAttribute('original-url') || element.getAttribute('data'),
            'object', element.getAttribute('type') || '');
        } else if (tag === 'embed') {
          add(element.getAttribute('original-url') || element.getAttribute('src'),
            'embed', element.getAttribute('type') || '');
        } else if (tag === 'iframe') {
          add(element.getAttribute('src'), 'iframe', element.getAttribute('type') || '');
        }
      }
      if (element.shadowRoot) scan(element.shadowRoot);
    }
  };
  try { scan(document); } catch (_) { /* inaccessible DOM fragment */ }
  const current = new URL(location.href);
  if (/application\/pdf/i.test(document.contentType) || /\.pdf$/i.test(current.pathname)) {
    add(current.href, 'document', document.contentType || '');
  }
  if (/\/(?:pdfjs\/)?viewer\.html$/i.test(current.pathname)) {
    const file = current.searchParams.get('file');
    if (file) add(file, 'pdfjs-file', 'application/pdf');
  }
  return targets;
}"""
_READ_BLOB_PDF_SCRIPT = """async (url) => {
  const response = await fetch(url);
  if (!response.ok) throw new Error('PDF blob is unavailable');
  return Array.from(new Uint8Array(await response.arrayBuffer()));
}"""
_PDF_CAPTURE_TIMEOUT_MS = 15_000
PLAYWRIGHT_INSTALL_HINT = (
    "install the optional browser support with `pip install playwright`; "
    "Google Chrome must be installed separately"
)
_CLOSED_ACCESS_MARKERS = (
    "access denied", "institutional access", "sign in", "login", "log in",
    "subscription", "purchase", "paywall", "paywalled",
)
_CAPTURE_TOOLBAR_SCRIPT = """
(() => {
  if (window !== window.top) return;
  window.__callimachusCaptureRequests = window.__callimachusCaptureRequests || [];
  const theme = '__CALLIMACHUS_THEME__';
  const place = (host) => {
    const viewport = window.visualViewport;
    const width = viewport ? viewport.width : window.innerWidth;
    const height = viewport ? viewport.height : window.innerHeight;
    const left = viewport ? viewport.offsetLeft : 0;
    const top = viewport ? viewport.offsetTop : 0;
    const gap = 12;
    const rect = host.getBoundingClientRect();
    const panelWidth = Math.min(246, Math.max(0, width - gap * 2));
    const panelHeight = Math.min(rect.height || 166, Math.max(0, height - gap * 2));
    const x = Number.isFinite(host.__captureX) ? host.__captureX : left + gap;
    const y = Number.isFinite(host.__captureY) ? host.__captureY : top + gap;
    host.__captureX = Math.max(left, Math.min(x, left + width - panelWidth));
    host.__captureY = Math.max(top, Math.min(y, top + height - panelHeight));
    host.style.setProperty('position', 'fixed', 'important');
    host.style.setProperty('inset', 'auto', 'important');
    host.style.setProperty('left', `${host.__captureX}px`, 'important');
    host.style.setProperty('top', `${host.__captureY}px`, 'important');
    host.style.setProperty('margin', '0', 'important');
    host.style.setProperty('width', `${panelWidth}px`, 'important');
    host.style.setProperty('height', 'auto', 'important');
    host.style.setProperty('max-height', `${Math.max(0, height - gap * 2)}px`, 'important');
    host.style.setProperty('box-sizing', 'border-box', 'important');
    host.style.setProperty('border', '0', 'important');
    host.style.setProperty('padding', '0', 'important');
    host.style.setProperty('background', 'transparent', 'important');
    host.style.setProperty('overflow', 'auto', 'important');
    host.style.setProperty('z-index', '2147483647', 'important');
    host.style.setProperty('display', 'block', 'important');
    host.style.setProperty('visibility', 'visible', 'important');
    host.style.setProperty('opacity', '1', 'important');
    host.style.setProperty('pointer-events', 'auto', 'important');
  };
  const show = (host) => {
    if (typeof host.showPopover === 'function' && !host.matches(':popover-open')) {
      try { host.showPopover(); } catch (_) { /* z-index fallback */ }
    }
  };
  if (!window.__callimachusCapturePositionBound) {
    const reposition = () => {
      const host = document.getElementById('callimachus-capture-toolbar');
      if (host) place(host);
    };
    window.addEventListener('resize', reposition);
    window.addEventListener('orientationchange', reposition);
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', reposition);
      window.visualViewport.addEventListener('scroll', reposition);
    }
    window.__callimachusCapturePositionBound = true;
  }
  const install = () => {
    if (!document.documentElement) return;
    let host = document.getElementById('callimachus-capture-toolbar');
    if (host) {
      if (host.parentNode !== document.documentElement) {
        document.documentElement.appendChild(host);
      }
      host.dataset.theme = theme;
      place(host);
      show(host);
      window.__callimachusCaptureToolbar = true;
      return;
    }
    host = document.createElement('callimachus-capture-toolbar');
    host.id = 'callimachus-capture-toolbar';
    host.setAttribute('popover', 'manual');
    host.dataset.theme = theme;
    place(host);
    const root = host.attachShadow({mode: 'closed'});
    const style = document.createElement('style');
    style.textContent = `
      :host { --surface:#fff; --text:#18202a; --muted:#52606d; --border:#cbd5e1; --button:#eaf2ff; --hover:#dcecff; --pressed:#163f75; --accent:#2457a6; font:13px/1.4 'Segoe UI',sans-serif; color:var(--text); }
      :host([data-theme="dark"]) { --surface:#101722; --text:#edf3ff; --muted:#aeb8c7; --border:#364152; --button:#1f2b3d; --hover:#2d4d73; --pressed:#183d78; --accent:#7aa7e8; }
      .panel { box-sizing:border-box; width:100%; padding:10px; background:var(--surface); border:1px solid var(--border); border-radius:12px; box-shadow:0 12px 32px rgba(0,0,0,.28); }
      .handle { padding:2px 2px 7px; cursor:grab; touch-action:none; user-select:none; border-bottom:1px solid var(--border); }
      .handle:active { cursor:grabbing; }
      .title { font-weight:700; color:var(--accent); }
      .hint { font-size:11px; color:var(--muted); }
      .description { margin:8px 2px; color:var(--text); }
      .actions { display:flex; gap:6px; }
      .text-actions { margin-top:6px; }
      button { flex:1; min-width:0; min-height:38px; padding:6px; background:var(--button); color:var(--text); border:1px solid var(--border); border-radius:7px; font:600 12px 'Segoe UI',sans-serif; cursor:pointer; transition:background .12s ease, transform .12s ease, border-color .12s ease; }
      button:hover { background:var(--hover); border-color:var(--accent); }
      button:active { background:var(--pressed); color:#fff; transform:translateY(1px); }
      button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
    `;
    const panel = document.createElement('div');
    panel.className = 'panel';
    const handle = document.createElement('div');
    handle.className = 'handle';
    handle.innerHTML = '<div class="title">Callimachus · Guided Fetch</div><div class="hint">Drag this panel to move it</div>';
    const description = document.createElement('div');
    description.className = 'description';
    description.textContent = 'Capture the page you are viewing, then confirm it in Callimachus.';
    const actions = document.createElement('div');
    actions.className = 'actions';
    for (const kind of ['html', 'pdf']) {
      const button = document.createElement('button');
      button.textContent = kind === 'html' ? 'Capture HTML' : 'Capture PDF';
      button.onclick = () => window.__callimachusCaptureRequests.push(kind);
      actions.appendChild(button);
    }
    let textActions = null;
    if (__CALLIMACHUS_TEXT_CAPTURE__) {
      textActions = document.createElement('div');
      textActions.className = 'actions text-actions';
      const button = document.createElement('button');
      button.textContent = 'Capture plain text';
      button.onclick = () => window.__callimachusCaptureRequests.push('text');
      textActions.appendChild(button);
    }
    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const originX = host.__captureX;
      const originY = host.__captureY;
      const startX = event.clientX;
      const startY = event.clientY;
      handle.setPointerCapture(event.pointerId);
      const move = (next) => {
        host.__captureX = originX + next.clientX - startX;
        host.__captureY = originY + next.clientY - startY;
        place(host);
      };
      const stop = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', stop);
        handle.removeEventListener('pointercancel', stop);
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', stop);
      handle.addEventListener('pointercancel', stop);
    });
    panel.appendChild(handle);
    panel.appendChild(description);
    panel.appendChild(actions);
    if (textActions) panel.appendChild(textActions);
    root.appendChild(style);
    root.appendChild(panel);
    document.documentElement.appendChild(host);
    place(host);
    show(host);
    window.__callimachusCaptureToolbar = true;
  };
  install();
  if (!window.__callimachusCaptureToolbar) {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  }
})();
"""


def _capture_toolbar_script(*, dark: bool = False, include_text: bool = True) -> str:
    return (
        _CAPTURE_TOOLBAR_SCRIPT
        .replace('__CALLIMACHUS_THEME__', 'dark' if dark else 'light')
        .replace('__CALLIMACHUS_TEXT_CAPTURE__', 'true' if include_text else 'false')
    )


_CLIPBOARD_WRITE_TEXT_SCRIPT = "nonce => navigator.clipboard.writeText(nonce)"
_CLIPBOARD_READ_TEXT_SCRIPT = "() => navigator.clipboard.readText()"
_CLEAR_TEXT_CAPTURE_NONCE_SCRIPT = """nonce => navigator.clipboard.readText().then(
  current => current === nonce ? navigator.clipboard.writeText('') : undefined
)"""
_PREPARE_TEXT_CAPTURE_SCRIPT = """() => {
  const key = '__callimachusTextCaptureRestoreState';
  const host = document.getElementById('callimachus-capture-toolbar');
  const body = document.body;
  if (!body) return false;
  const active = document.activeElement;
  window[key] = {
    host,
    hostStyles: host ? ['visibility', 'opacity', 'pointer-events'].map(name => [
      name, host.style.getPropertyValue(name), host.style.getPropertyPriority(name)
    ]) : [],
    body,
    bodyHadTabindex: body.hasAttribute('tabindex'),
    bodyTabindex: body.getAttribute('tabindex'),
    active
  };
  if (host) {
    host.style.setProperty('visibility', 'hidden', 'important');
    host.style.setProperty('opacity', '0', 'important');
    host.style.setProperty('pointer-events', 'none', 'important');
  }
  if (active && active !== body && typeof active.blur === 'function') active.blur();
  if (!body.hasAttribute('tabindex')) body.setAttribute('tabindex', '-1');
  body.focus({preventScroll: true});
  return document.activeElement === body;
}"""
_RESTORE_TEXT_CAPTURE_SCRIPT = """() => {
  const key = '__callimachusTextCaptureRestoreState';
  const state = window[key];
  if (!state) return;
  if (state.host && state.host.isConnected) {
    for (const [name, value, priority] of state.hostStyles) {
      if (value) state.host.style.setProperty(name, value, priority);
      else state.host.style.removeProperty(name);
    }
  }
  if (state.body && state.body.isConnected) {
    if (state.bodyHadTabindex) state.body.setAttribute('tabindex', state.bodyTabindex);
    else state.body.removeAttribute('tabindex');
  }
  delete window[key];
  if (state.active && state.active.isConnected && typeof state.active.focus === 'function') {
    state.active.focus({preventScroll: true});
  }
}"""
_CAPTURE_TOOLBAR_VISIBLE_SCRIPT = """() => {
  const host = document.getElementById('callimachus-capture-toolbar');
  if (!host) return false;
  const box = host.getBoundingClientRect();
  const style = getComputedStyle(host);
  const viewport = window.visualViewport;
  const left = viewport ? viewport.offsetLeft : 0;
  const top = viewport ? viewport.offsetTop : 0;
  const right = left + (viewport ? viewport.width : window.innerWidth);
  const bottom = top + (viewport ? viewport.height : window.innerHeight);
  return box.width > 0 && box.height > 0 &&
    box.left < right && box.right > left &&
    box.top < bottom && box.bottom > top &&
    style.display !== 'none' && style.visibility !== 'hidden' &&
    style.opacity !== '0';
}"""
_CLOSED_TARGET_MARKERS = (
    "target page, context or browser has been closed",
    "target page, context or browser is closed",
    "browser has been closed",
    "page has been closed",
    "context has been closed",
)

_VISIBLE_SHADOW_HTML_SCRIPT = """() => {
  const contents = [];
  const visit = (root) => {
    for (const element of root.querySelectorAll('*')) {
      if (!element.shadowRoot || !element.getClientRects().length ||
          getComputedStyle(element).visibility === 'hidden') continue;
      contents.push(element.shadowRoot.innerHTML);
      visit(element.shadowRoot);
    }
  };
  visit(document);
  return contents;
}"""


def _insert_rendered_content(html: str, sections: list[str]) -> str:
    """Keep the current DOM and make visible embedded DOM readable offline."""
    if not sections:
        return html
    embedded = "<section data-callimachus-rendered-content>" + "\n".join(sections) + "</section>"
    if re.search(r"</body\s*>", html, flags=re.I):
        return re.sub(r"</body\s*>", lambda match: embedded + match.group(), html, count=1, flags=re.I)
    return html + embedded


def _rendered_shadow_sections(contents, *, origin: str) -> list[str]:
    if not isinstance(contents, list):
        return []
    safe_origin = html_mod.escape(origin, quote=True)
    return [
        f'<section data-callimachus-shadow-origin="{safe_origin}">{markup}</section>'
        for markup in contents if isinstance(markup, str) and markup.strip()
    ]


def _capture_rendered_html(page) -> str:
    """Snapshot the current DOM, including visible frame and open-shadow content."""
    html = page.content()
    sections = _rendered_shadow_sections(
        page.evaluate(_VISIBLE_SHADOW_HTML_SCRIPT), origin=page.url,
    )
    for frame in page.frames:
        if frame == page.main_frame or not frame.frame_element().is_visible():
            continue
        frame_url = html_mod.escape(frame.url, quote=True)
        body = frame.locator("body").inner_html()
        if body.strip():
            sections.append(
                f'<section data-callimachus-frame-url="{frame_url}">{body}</section>'
            )
        sections.extend(_rendered_shadow_sections(
            frame.evaluate(_VISIBLE_SHADOW_HTML_SCRIPT), origin=frame.url,
        ))
    return _insert_rendered_content(html, sections)


async def _capture_rendered_html_async(page) -> str:
    html = await page.content()
    sections = _rendered_shadow_sections(
        await page.evaluate(_VISIBLE_SHADOW_HTML_SCRIPT), origin=page.url,
    )
    for frame in page.frames:
        if frame == page.main_frame or not await (await frame.frame_element()).is_visible():
            continue
        frame_url = html_mod.escape(frame.url, quote=True)
        body = await frame.locator("body").inner_html()
        if body.strip():
            sections.append(
                f'<section data-callimachus-frame-url="{frame_url}">{body}</section>'
            )
        sections.extend(_rendered_shadow_sections(
            await frame.evaluate(_VISIBLE_SHADOW_HTML_SCRIPT), origin=frame.url,
        ))
    return _insert_rendered_content(html, sections)


class InteractiveBrowserClosed(RuntimeError):
    """The operator closed the visible Chrome session."""


class InteractiveCaptureCancelled(RuntimeError):
    """An operator cancelled a Guided Fetch browser capture."""


def groups(need, refs, fetch_results):
    """Use the established challenge detection/grouping policy unchanged."""
    return browser_challenge.groups(need, refs, fetch_results)


def _number_env(name: str, default, *, minimum):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _headed_gui_available() -> bool:
    """Cheap preflight; launch errors remain the authoritative fallback signal."""
    if sys.platform == "darwin" or os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _chrome_distribution_missing(exc: Exception) -> bool:
    """Recognize only Playwright's explicit system-Chrome absence errors."""
    message = str(exc).lower()
    return (
        "chromium distribution 'chrome' is not found" in message
        or 'chromium distribution "chrome" is not found' in message
        or (
            "executable doesn't exist" in message
            and ("google/chrome" in message or "google\\chrome" in message)
        )
    )


def _chrome_profile_in_use(exc: Exception) -> bool:
    """Recognize Chrome's explicit single-profile-instance startup failures."""
    message = " ".join(str(exc).lower().split())
    if re.search(r"<process did exit: exitcode=(?:21|24)\b", message):
        return True
    if any(marker in message for marker in (
        "processsingleton",
        "singletonlock",
        "opening in existing browser session",
    )):
        return True
    profile_in_use = any(marker in message for marker in (
        "already in use",
        "appears to be in use",
        "is in use",
    ))
    return profile_in_use and any(marker in message for marker in (
        "user-data-dir",
        "user data directory",
        "profile",
    ))


def _wsl_runtime() -> bool:
    """Return whether this Linux process is running under WSL."""
    return sys.platform.startswith("linux") and "microsoft" in os.uname().release.lower()


def _chrome_distribution_reason(exc: Exception) -> str:
    message = f"Google Chrome unavailable: {exc}; Chrome is an external prerequisite"
    if _wsl_runtime():
        message += "; this Linux/WSL process needs Linux Google Chrome and cannot use Windows chrome.exe"
    return message


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "source").strip("._") or "source"


def _pdf_display_name(value: str | None) -> str:
    """Return a safe, explicit PDF name for a verified browser download."""
    filename = _safe_name(Path(value or "download").name)
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    return f"{stem or 'download'}.pdf"


def _challenge_active(page) -> bool:
    try:
        return fetch_html.is_challenge_html(page.content()) or fetch_html.is_challenge_url(page.url)
    except Exception:
        return True


def _wait_for_user(page, *, timeout: float, poll_seconds: float) -> bool:
    """Wait until challenge markers disappear; no CAPTCHA interaction is automated."""
    deadline = time.monotonic() + timeout
    while _challenge_active(page):
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(int(min(poll_seconds, max(0.1, deadline - time.monotonic())) * 1000))
    return True


def _wait_for_legitimate_access(
    page,
    *,
    initial_url: str,
    initial_html: str,
    timeout: float,
    poll_seconds: float,
) -> bool:
    """Leave a visible, isolated browser for a user to authenticate.

    This performs no login or challenge action.  A challenge must disappear;
    otherwise a navigation or DOM change is the only signal that the user has
    taken an access action.  The normal fetch/identity gates decide whether
    the resulting document is actually usable.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            current_html = page.content()
            if not fetch_html.is_challenge_html(current_html) and not fetch_html.is_challenge_url(page.url):
                if page.url != initial_url or current_html != initial_html:
                    return True
                closed = fetch_html.is_paywalled_html(current_html) or any(
                    marker in current_html.lower() for marker in _CLOSED_ACCESS_MARKERS
                )
                if not closed:
                    return True
        except Exception:
            return False
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(int(min(poll_seconds, max(0.1, deadline - time.monotonic())) * 1000))


def _write_artifact(directory: Path, ref_id: str, index: int, suffix: str, body: bytes) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_name(ref_id)}-{index}{suffix}"
    path.write_bytes(body)
    return str(path)


def _normalize_pdf_target(value: str, base_url: str) -> str | None:
    """Resolve a viewer URL and drop display-only fragments before retrieval."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        resolved = urljoin(base_url or "", value.strip())
        parsed = urlsplit(resolved)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme == "blob":
        return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _write_download_artifact(
    directory: Path, ref_id: str, index: int, filename: str | None, body: bytes,
) -> str:
    """Materialize a confirmed download with a readable, collision-safe PDF name."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_name(ref_id)}-{index}-{_pdf_display_name(filename)}"
    path.write_bytes(body)
    return str(path)


def _save_download(download, path: Path) -> bytes:
    """Materialize a Playwright Download; kept separate for lightweight mocks."""
    download.save_as(str(path))
    return path.read_bytes()


class InteractiveBrowserSession:
    """A visible, user-operated session backed by the system Google Chrome.

    Playwright is deliberately only a control library here.  ``channel="chrome"``
    means neither a bundled Chromium nor a ``playwright install`` download is
    ever used.  An instance must be created, used and closed from one thread.
    """

    def __init__(self, run_dir: str, *, profile_id: str | None = None):
        self.run_dir = Path(run_dir)
        self.profile_id = profile_id
        self._persistent_profile = profile_id is not None
        self.profile_dir: Path | None = None
        self.artifacts_dir = self.run_dir / "browser_interactive_artifacts"
        self.available = False
        self.reason = None
        self.reason_code = None
        self._playwright = None
        self._context = None
        self.page = None
        self._pdf_responses = []
        self._downloads = []
        self._requested_url = None
        self._last_response = None

    def _install_capture_toolbar(self) -> None:
        """Install an isolated UI that only queues explicit user capture requests."""
        if self.page is not None:
            self.page.evaluate(_capture_toolbar_script(include_text=False))

    @staticmethod
    def _install_toolbar_on_page(page) -> None:
        try:
            page.evaluate(_capture_toolbar_script(include_text=False))
        except Exception:
            pass

    def drain_capture_requests(self) -> list[str]:
        if not self.available or self._context is None:
            return []
        captured: list[str] = []
        for page in reversed(self._context.pages):
            try:
                self._install_toolbar_on_page(page)
                requests = page.evaluate(
                    "() => { const q = window.__callimachusCaptureRequests || []; "
                    "window.__callimachusCaptureRequests = []; return q; }"
                )
            except Exception:
                continue
            valid = (
                [item for item in requests if item in {"html", "pdf"}]
                if isinstance(requests, list)
                else []
            )
            if valid:
                # Capture the page on which the operator clicked, including a
                # newly opened publisher tab.
                self.page = page
                captured.extend(valid)
        return captured

    def _unavailable(self, code: str, message: str) -> dict:
        self.available = False
        self.reason_code = code
        self.reason = message
        return {"available": False, "reason_code": code, "reason": message}

    def start(self) -> dict:
        """Start the external Chrome session, returning an explicit failure state."""
        if self.available:
            return {"available": True, "reason_code": None, "reason": None}
        if not _headed_gui_available():
            return self._unavailable("headed_display_unavailable", "no headed GUI display available")
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            return self._unavailable(
                "playwright_unavailable",
                f"Playwright unavailable: {exc}; {PLAYWRIGHT_INSTALL_HINT}",
            )
        try:
            if not self.run_dir.is_dir():
                raise FileNotFoundError("run directory is unavailable")
            if self._persistent_profile:
                self.profile_dir = institutional_access.profile_directory(self.profile_id)
                self.profile_dir.mkdir(parents=True, exist_ok=True)
            else:
                self.profile_dir = Path(tempfile.mkdtemp(
                    prefix="citation-verifier-chrome-",
                    dir=self.run_dir,
                ))
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="chrome",
                headless=False,
                accept_downloads=True,
                chromium_sandbox=True,
                no_viewport=True,
            )
            self._context.add_init_script(script=_capture_toolbar_script(include_text=False))
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self.page.on(
                "response",
                lambda response: self._pdf_responses.append(response)
                if "application/pdf" in response.headers.get("content-type", "").lower()
                else None,
            )
            self.page.on("response", self._record_navigation_response)
            self.page.on("download", lambda download: self._downloads.append(download))
            self._context.on("page", self._install_toolbar_on_page)
            self._install_capture_toolbar()
        except Exception as exc:
            self.close()
            if _chrome_profile_in_use(exc):
                return self._unavailable(
                    "browser_profile_in_use",
                    "The institutional Chrome profile is already in use. "
                    "Close the other Chrome window using this profile, then retry.",
                )
            return self._unavailable(
                "google_chrome_unavailable",
                f"Google Chrome unavailable: {exc}; Chrome is an external prerequisite",
            )
        self.available = True
        self.reason = None
        self.reason_code = None
        return {"available": True, "reason_code": None, "reason": None}

    def _record_navigation_response(self, response) -> None:
        """Keep final document metadata current after publisher UI navigation."""
        try:
            request = response.request
            if not request.is_navigation_request():
                return
            if response.frame is not self.page.main_frame:
                return
        except Exception:
            return
        self._last_response = response

    def reset_captures(self) -> None:
        self._pdf_responses.clear()
        self._downloads.clear()

    def open(self, url: str):
        """Navigate the visible tab.  This does not automate any login/CAPTCHA."""
        if not self.available or self.page is None:
            raise RuntimeError("interactive browser session is not available")
        try:
            parsed = urlparse(str(url))
        except ValueError as exc:
            raise ValueError("interactive browser URL is invalid") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("interactive browser requires an HTTP(S) URL without credentials")
        self.reset_captures()
        self._requested_url = str(url)
        try:
            self._last_response = self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            # A direct PDF download can raise after its download event is emitted.
            if not self._downloads:
                raise RuntimeError(f"interactive browser navigation failed: {exc}") from exc
            self._last_response = None
        self._install_capture_toolbar()
        response = self._last_response
        return {
            "requested_url": self._requested_url,
            "url": self.page.url,
            "status": getattr(response, "status", None),
            "content_type": getattr(response, "headers", {}).get("content-type"),
        }

    def capture_current_pdf(self, ref_id: str, index: int):
        if not self.available or self.page is None:
            raise RuntimeError("interactive browser session is not available")
        # Playwright's synchronous dispatcher advances only during a
        # Playwright API call. Pump it so events from a manual browser action
        # reach the response/download handlers before their queues are read.
        self.page.wait_for_timeout(0)
        captured = _captured_pdf(
            self._pdf_responses, self._downloads, self.artifacts_dir, ref_id, index
        )
        if captured is None:
            return None
        return {
            "ref_id": ref_id,
            "capture_index": index,
            "requested_url": self._requested_url,
            **captured,
        }

    def capture_current_html(self, ref_id: str, index: int, *, response=None):
        if not self.available or self.page is None:
            raise RuntimeError("interactive browser session is not available")
        html = _capture_rendered_html(self.page)
        if fetch_html.is_challenge_html(html):
            return None
        headers = getattr(response, "headers", {}) if response is not None else {}
        content_type = headers.get("content-type", "").lower()
        return {
            "ref_id": ref_id,
            "capture_index": index,
            "artifact_path": _write_artifact(
                self.artifacts_dir, ref_id, index, ".html", html.encode("utf-8")
            ),
            "url": self.page.url,
            "requested_url": self._requested_url,
            "status": getattr(response, "status", 200),
            "content_type": content_type or "text/html",
            "body": html.encode("utf-8"),
        }

    def close(self) -> None:
        """Close runtime handles, removing only an ephemeral profile."""
        context, playwright, profile = self._context, self._playwright, self.profile_dir
        self._context = None
        self._playwright = None
        self.page = None
        self.profile_dir = None
        self._requested_url = None
        self._last_response = None
        self.available = False
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        if profile is not None and not self._persistent_profile:
            shutil.rmtree(profile, ignore_errors=True)


class AsyncInteractiveBrowserSession:
    """Thread-safe facade for the Guided Fetch browser only.

    The async Playwright objects live exclusively on the private event-loop
    thread.  Qt invokes this facade synchronously from its worker thread, so
    no Playwright dispatcher or asyncio loop crosses a Qt callback boundary.
    """

    def __init__(
        self,
        run_dir: str | None = None,
        *,
        capture_toolbar: bool = True,
        login_only: bool = False,
        profile_id: str | None = None,
    ):
        if run_dir is None and not login_only:
            raise ValueError("a run directory is required when browser capture is enabled")
        self.login_only = bool(login_only)
        self.run_dir = (
            Path(run_dir) if run_dir is not None and not self.login_only else None
        )
        self.profile_id = profile_id
        self.profile_dir: Path | None = None
        self.artifacts_dir = (
            self.run_dir / "browser_interactive_artifacts"
            if self.run_dir is not None else None
        )
        self._capture_toolbar = bool(capture_toolbar) and not self.login_only
        self.available = False
        self.reason = None
        self.reason_code = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._capture_lock = threading.Lock()
        self._active_capture_key: tuple[str, int] | None = None
        self._active_capture_task = None
        self._capture_cancellations: set[tuple[str, int]] = set()
        self._capture_cancel_before_start: set[tuple[str, int]] = set()
        self._last_capture_key: tuple[str, int] | None = None
        self._playwright = self._context = self.page = None
        self._pdf_responses = []
        self._downloads = []
        self._page_listener_ids: set[int] = set()
        self._download_records = {}
        self._next_download_token = 1
        self._requested_url = self._last_response = None
        self._stale = False
        self._toolbar_failures: dict[int, int] = {}
        self._toolbar_reported: set[int] = set()
        self._toolbar_status = CaptureToolbarStatus.READY
        self._dark_theme = False

    def set_theme(self, dark: bool) -> None:
        """Apply the GUI theme to current pages and future navigations."""
        self._dark_theme = bool(dark)
        if self.available:
            self._call(self._apply_theme())

    async def _apply_theme(self) -> None:
        if not self._capture_toolbar or self._context is None:
            return
        script = _capture_toolbar_script(dark=self._dark_theme)
        for page in self._context.pages:
            try:
                await page.evaluate(script)
            except Exception:
                pass

    def _unavailable(self, code: str, message: str) -> dict:
        self.available, self.reason_code, self.reason = False, code, message
        return {"available": False, "reason_code": code, "reason": message}

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        loop.close()

    def _call(self, coroutine):
        if self._loop is None:
            raise RuntimeError("interactive browser event loop is not available")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def cancel_capture(self, ref_id: str, index: int) -> None:
        """Cancel one active capture without touching Chrome or its event loop directly."""
        key = (str(ref_id), int(index))
        with self._capture_lock:
            if self._active_capture_key == key:
                self._capture_cancellations.add(key)
                task = self._active_capture_task
                loop = self._loop
            elif self._last_capture_key == key:
                return
            else:
                self._capture_cancel_before_start.add(key)
                return
        if task is not None and loop is not None:
            loop.call_soon_threadsafe(task.cancel)

    def _call_capture(self, coroutine, ref_id: str, index: int):
        """Run one keyed capture and wait for its async cleanup before returning."""
        key = (str(ref_id), int(index))
        if self._loop is None:
            with self._capture_lock:
                cancelled = key in self._capture_cancel_before_start
                self._capture_cancel_before_start.discard(key)
            if cancelled:
                coroutine.close()
                raise InteractiveCaptureCancelled("browser capture cancelled")
            return self._call(coroutine)

        with self._capture_lock:
            if key in self._capture_cancel_before_start:
                self._capture_cancel_before_start.discard(key)
                self._last_capture_key = key
                coroutine.close()
                raise InteractiveCaptureCancelled("browser capture cancelled")
            if self._active_capture_key is not None:
                coroutine.close()
                raise RuntimeError("another browser capture is already active")
            self._active_capture_key = key
            self._active_capture_task = None

        async def run_capture():
            task = asyncio.current_task()
            with self._capture_lock:
                self._active_capture_task = task
                cancel_before_start = key in self._capture_cancellations
            try:
                if cancel_before_start:
                    task.cancel()
                return await coroutine
            except asyncio.CancelledError as exc:
                raise InteractiveCaptureCancelled("browser capture cancelled") from exc
            finally:
                with self._capture_lock:
                    self._capture_cancellations.discard(key)
                    self._capture_cancel_before_start.discard(key)
                    if self._active_capture_key == key:
                        self._active_capture_key = None
                        self._active_capture_task = None
                    self._last_capture_key = key

        try:
            return self._call(run_capture())
        except InteractiveCaptureCancelled:
            raise
        except BaseException:
            with self._capture_lock:
                self._capture_cancellations.discard(key)
                if self._active_capture_key == key:
                    self._active_capture_key = None
                    self._active_capture_task = None
                self._last_capture_key = key
            raise

    def start(self) -> dict:
        if self.available:
            return {"available": True, "reason_code": None, "reason": None}
        if not _headed_gui_available():
            return self._unavailable("headed_display_unavailable", "no headed GUI display available")
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            return self._unavailable(
                "playwright_unavailable",
                f"Playwright unavailable: {exc}; {PLAYWRIGHT_INSTALL_HINT}",
            )
        try:
            if self.run_dir is not None and not self.run_dir.is_dir():
                raise FileNotFoundError("run directory is unavailable")
            self.profile_dir = institutional_access.profile_directory(self.profile_id)
            self.profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return self._unavailable("browser_profile_unavailable", f"Chrome profile unavailable: {exc}")
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_loop, name="guided-fetch-playwright", daemon=True)
        self._thread.start()
        self._ready.wait()
        try:
            return self._call(self._start(async_playwright))
        except Exception as exc:
            self.close()
            if _chrome_distribution_missing(exc):
                return self._unavailable("google_chrome_unavailable", _chrome_distribution_reason(exc))
            if _chrome_profile_in_use(exc):
                return self._unavailable(
                    "browser_profile_in_use",
                    "The Guided Fetch Chrome profile is already in use. "
                    "Close the other Chrome window using this profile, then retry.",
                )
            return self._unavailable("browser_launch_failed", f"Google Chrome could not be started: {exc}")

    async def _start(self, async_playwright) -> dict:
        self._playwright = await async_playwright().start()
        return await self._launch_context()

    async def _launch_context(self) -> dict:
        """Launch Chrome against the persistent Guided Fetch profile."""
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir), channel="chrome", headless=False,
            accept_downloads=not self.login_only,
            chromium_sandbox=True,
            no_viewport=True,
        )
        if self._capture_toolbar:
            await self._context.add_init_script(script=_capture_toolbar_script(dark=self._dark_theme))
        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        if not self.login_only:
            self._attach_page_events(self.page)
        if self._capture_toolbar:
            self._context.on("page", self._install_toolbar_on_page)
            await self._install_capture_toolbar()
        self.available, self.reason, self.reason_code = True, None, None
        self._stale = False
        return {"available": True, "reason_code": None, "reason": None}

    def restart(self) -> dict:
        """Reopen Chrome after an operator closes it, retaining its profile."""
        if self._loop is None or self._playwright is None or self.profile_dir is None:
            raise RuntimeError("interactive browser session is not available")
        return self._call(self._restart())

    def park(self) -> None:
        """Close the visible Chrome context while retaining its worker and profile."""
        if self._loop is None or self._playwright is None or self.profile_dir is None:
            raise RuntimeError("interactive browser session is not available")
        self._call(self._park())

    async def _park(self) -> None:
        """Close only the current context; a later open restarts on this profile."""
        await self._close_visible_context()

    def clear_browser_cache(self) -> dict:
        """Clear only this active profile's HTTP cache through its CDP session."""
        if self._loop is None:
            return {
                "ok": False, "cleared": 0, "deferred": 0,
                "errors": ["browser_session_unavailable"],
            }
        try:
            return self._call(self._clear_browser_cache())
        except Exception:
            return {
                "ok": False, "cleared": 0, "deferred": 0,
                "errors": ["browser_cache_operation_failed"],
            }

    async def _clear_browser_cache(self) -> dict:
        context, page = self._context, self.page
        if not self.available or context is None or page is None:
            return {
                "ok": False, "cleared": 0, "deferred": 0,
                "errors": ["browser_session_unavailable"],
            }
        cdp_session = None
        cleared = 0
        errors = []
        try:
            cdp_session = await context.new_cdp_session(page)
            await cdp_session.send("Network.clearBrowserCache")
            cleared = 1
        except Exception:
            errors.append("cdp_cache_clear_failed")
        finally:
            if cdp_session is not None:
                try:
                    await cdp_session.detach()
                except Exception:
                    errors.append("cdp_session_detach_failed")
        return {
            "ok": cleared == 1 and not errors,
            "cleared": cleared,
            "deferred": 0,
            "errors": errors,
        }

    async def _close_visible_context(self) -> None:
        context = self._context
        self._context = None
        self.page = None
        self.available = False
        self._stale = True
        self._pdf_responses.clear()
        self._downloads.clear()
        self._download_records.clear()
        self._page_listener_ids.clear()
        self._toolbar_failures.clear()
        self._toolbar_reported.clear()
        self._toolbar_status = CaptureToolbarStatus.READY
        self._requested_url = self._last_response = None
        if context is not None:
            await context.close()

    async def _restart(self) -> dict:
        """Replace only the closed persistent context; credentials stay on disk."""
        try:
            await self._close_visible_context()
        except Exception:
            pass
        return await self._launch_context()

    def _record_download(self, download):
        self._downloads.append(download)
        self._download_records[id(download)] = {
            "download": download,
            "claimed": False,
            "discarded": False,
            "noticed": False,
        }

    def _attach_page_events(self, page) -> None:
        if self.login_only:
            return
        page_id = id(page)
        if page_id in self._page_listener_ids:
            return
        self._page_listener_ids.add(page_id)
        page.on("response", lambda response, owner=page: self._record_pdf_response(response, owner))
        page.on("download", self._record_download)

    async def _download_body(self, record):
        body = record.get("body")
        if body is not None:
            return body
        path = await record["download"].path()
        body = Path(path).read_bytes()
        record["body"] = body
        return body

    def drain_download_notices(self) -> list[dict]:
        if self.login_only:
            return []
        return self._call(self._drain_download_notices()) if self.available else []

    async def _drain_download_notices(self) -> list[dict]:
        notices = []
        for record in self._download_records.values():
            if record["noticed"] or record["claimed"] or record["discarded"]:
                continue
            try:
                body = await self._download_body(record)
            except Exception:
                continue
            if not body.startswith(b"%PDF"):
                record["discarded"] = True
                continue
            token = self._next_download_token
            self._next_download_token += 1
            record["token"] = token
            record["noticed"] = True
            download = record["download"]
            notices.append({
                "token": token,
                "suggested_filename": _pdf_display_name(
                    getattr(download, "suggested_filename", None)
                ),
                "url": getattr(download, "url", None) or None,
            })
        return notices

    def discard_download(self, token: int) -> None:
        if self.login_only:
            return
        self._call(self._discard_download(token))

    async def _discard_download(self, token: int) -> None:
        for record in self._download_records.values():
            if record.get("token") == token:
                record["discarded"] = True
                return

    def capture_download_pdf(self, ref_id: str, index: int, token: int):
        if self.login_only:
            raise RuntimeError("capture is disabled for a login-only browser session")
        return self._call_capture(
            self._capture_download_pdf(ref_id, index, token), ref_id, index
        )

    async def _capture_download_pdf(self, ref_id: str, index: int, token: int):
        for record in self._download_records.values():
            if record.get("token") != token:
                continue
            if record["claimed"] or record["discarded"]:
                return None
            try:
                body = await self._download_body(record)
            except Exception:
                return None
            if not body.startswith(b"%PDF"):
                return None
            record["claimed"] = True
            download = record["download"]
            display_name = _pdf_display_name(
                getattr(download, "suggested_filename", None)
            )
            return {
                "ref_id": ref_id,
                "capture_index": index,
                "requested_url": self._requested_url,
                "artifact_path": _write_download_artifact(
                    self.artifacts_dir, ref_id, index, display_name, body
                ),
                "url": getattr(download, "url", None),
                "status": 200,
                "content_type": "application/pdf",
                "body": body,
                "display_name": display_name,
            }
        return None

    def _record_pdf_response(self, response, page=None):
        if "application/pdf" in response.headers.get("content-type", "").lower():
            self._pdf_responses.append((page, response))

    def _install_toolbar_on_page(self, page):
        self._attach_page_events(page)
        if not self._capture_toolbar:
            return
        asyncio.create_task(self._install_toolbar(page))

    async def _install_toolbar(self, page):
        if not self._capture_toolbar:
            return False
        try:
            await page.evaluate(_capture_toolbar_script(dark=self._dark_theme))
            return bool(await page.evaluate(_CAPTURE_TOOLBAR_VISIBLE_SCRIPT))
        except Exception:
            return False

    async def _install_capture_toolbar(self):
        if self._capture_toolbar and self.page is not None:
            await self._install_toolbar(self.page)

    def open(self, url: str):
        return self._call(self._open(url))

    def open_login_pages(self, urls: Sequence[str]) -> dict:
        """Open a validated batch of login pages in this persistent context."""
        if not self.login_only:
            raise RuntimeError("login pages require a login-only browser session")
        validated = self._validate_login_pages(urls)
        return self._call(self._open_login_pages(validated))

    @staticmethod
    def _validate_login_pages(urls: Sequence[str]) -> list[str]:
        if isinstance(urls, (str, bytes)) or not isinstance(urls, Sequence):
            raise ValueError("login pages must be a sequence of stable HTTPS URLs")
        validated = []
        seen = set()
        for candidate in urls:
            if not isinstance(candidate, str):
                raise ValueError("login pages must be stable HTTPS URLs")
            try:
                url = institutional_access.config({
                    institutional_access.ENV_LOGIN_URL: candidate,
                })["login_url"]
            except (TypeError, ValueError) as exc:
                raise ValueError("login pages must be stable HTTPS URLs") from exc
            if not url:
                raise ValueError("login pages must be stable HTTPS URLs")
            if url not in seen:
                seen.add(url)
                validated.append(url)
        if not validated:
            raise ValueError("at least one login page is required")
        return validated

    async def _open_login_pages(self, urls: Sequence[str]) -> dict:
        """Navigate all login destinations on the existing Playwright loop."""
        if not self.login_only:
            raise RuntimeError("login pages require a login-only browser session")
        if not self.available or self._context is None or self.page is None:
            raise RuntimeError("interactive browser session is not available")
        if self._runtime_is_closed():
            raise self._mark_closed()

        pages = []
        failed = []
        for index, url in enumerate(urls):
            if index == 0:
                pages.append((url, self.page))
                continue
            try:
                page = await self._context.new_page()
            except Exception as exc:
                reason = (
                    "browser_closed"
                    if self._runtime_is_closed() or self._closed_target_error(exc)
                    else "navigation_failed"
                )
                if reason == "browser_closed":
                    self._stale = True
                failed.append({"url": url, "reason": reason})
                continue
            pages.append((url, page))

        async def navigate(url, page):
            try:
                await page.goto(url, wait_until="domcontentloaded")
                return {"url": url, "opened": True}
            except Exception as exc:
                reason = (
                    "browser_closed"
                    if self._runtime_is_closed() or self._closed_target_error(exc)
                    else "navigation_failed"
                )
                if reason == "browser_closed":
                    self._stale = True
                return {"url": url, "reason": reason}

        outcomes = await asyncio.gather(
            *(navigate(url, page) for url, page in pages)
        )
        opened = []
        for outcome in outcomes:
            if outcome.get("opened"):
                opened.append(outcome["url"])
            else:
                failed.append({"url": outcome["url"], "reason": outcome["reason"]})
        return {"opened": opened, "failed": failed}

    @property
    def stale(self) -> bool:
        """Whether the visible browser was closed outside the application."""
        return self._stale

    @property
    def toolbar_status(self) -> CaptureToolbarStatus:
        """Return the current nonfatal toolbar readiness state."""
        return self._toolbar_status

    def _runtime_is_closed(self) -> bool:
        if self.page is None:
            return True
        is_closed = getattr(self.page, "is_closed", None)
        if callable(is_closed):
            try:
                if is_closed():
                    return True
            except Exception:
                return True
        if self._context is not None:
            try:
                return not self._context.pages
            except Exception:
                return True
        return False

    def _mark_closed(self) -> InteractiveBrowserClosed:
        self._stale = True
        return InteractiveBrowserClosed("interactive browser was closed by the user")

    def _closed_target_error(self, exc: Exception) -> bool:
        return any(marker in str(exc).lower() for marker in _CLOSED_TARGET_MARKERS)

    async def _open(self, url: str):
        if not self.available or self.page is None:
            raise RuntimeError("interactive browser session is not available")
        parsed = urlparse(str(url))
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("interactive browser requires an HTTP(S) URL without credentials")
        if self._runtime_is_closed():
            raise self._mark_closed()
        self._pdf_responses.clear(); self._downloads.clear(); self._download_records.clear()
        self._requested_url = str(url)
        try:
            self._last_response = await self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            if self._runtime_is_closed() or self._closed_target_error(exc):
                raise self._mark_closed() from exc
            # Let Playwright dispatch a download event before deciding that a
            # failed navigation is a real error. Direct PDF downloads commonly
            # abort navigation after handing the file to the browser.
            try:
                await self.page.wait_for_timeout(0)
            except Exception as wait_exc:
                if self._runtime_is_closed() or self._closed_target_error(wait_exc):
                    raise self._mark_closed() from exc
                raise RuntimeError(f"interactive browser navigation failed: {exc}") from exc
            if not self._downloads:
                raise RuntimeError(f"interactive browser navigation failed: {exc}") from exc
            self._last_response = None
        await self._install_capture_toolbar()
        response = self._last_response
        return {"requested_url": self._requested_url, "url": self.page.url,
                "status": getattr(response, "status", None),
                "content_type": getattr(response, "headers", {}).get("content-type")}

    def drain_capture_requests(self) -> list[str]:
        return self._call(self._drain_capture_requests()) if self.available else []

    async def _drain_capture_requests(self) -> list[str]:
        if not self._capture_toolbar:
            return []
        captured = []
        toolbar_warning = False
        pages = list(self._context.pages)
        live_pages = {id(page) for page in pages}
        self._toolbar_reported.intersection_update(live_pages)
        for page_id in set(self._toolbar_failures) - live_pages:
            self._toolbar_failures.pop(page_id)
        for page in reversed(pages):
            visible = await self._install_toolbar(page)
            page_id = id(page)
            if visible:
                self._toolbar_failures.pop(page_id, None)
                self._toolbar_reported.discard(page_id)
            elif str(page.url).startswith(("http://", "https://")):
                failures = self._toolbar_failures.get(page_id, 0) + 1
                self._toolbar_failures[page_id] = failures
                if failures >= 5 and page_id not in self._toolbar_reported:
                    self._toolbar_reported.add(page_id)
                    toolbar_warning = True
            try:
                requests = await page.evaluate("() => { const q = window.__callimachusCaptureRequests || []; window.__callimachusCaptureRequests = []; return q; }")
            except Exception:
                continue
            if isinstance(requests, list):
                valid = [item for item in requests if item in {"html", "pdf", "text"}]
                if valid:
                    captured.extend(valid)
                    self.page = page
        if toolbar_warning and not captured:
            self._toolbar_status = CaptureToolbarStatus.PENDING
        elif (
            self._toolbar_status is CaptureToolbarStatus.PENDING
            and not self._toolbar_reported
        ):
            self._toolbar_status = CaptureToolbarStatus.READY
        return captured

    def capture_current_html(self, ref_id: str, index: int):
        if self.login_only:
            raise RuntimeError("capture is disabled for a login-only browser session")
        return self._call_capture(self._capture_current_html(ref_id, index), ref_id, index)

    async def _capture_current_html(self, ref_id, index):
        html = await _capture_rendered_html_async(self.page)
        if fetch_html.is_challenge_html(html):
            return None
        return {"ref_id": ref_id, "capture_index": index,
                "artifact_path": _write_artifact(self.artifacts_dir, ref_id, index, ".html", html.encode("utf-8")),
                "url": self.page.url, "requested_url": self._requested_url, "status": 200,
                "content_type": "text/html", "body": html.encode("utf-8")}

    def capture_current_text(self, ref_id: str, index: int):
        if self.login_only:
            raise RuntimeError("capture is disabled for a login-only browser session")
        return self._call_capture(self._capture_current_text(ref_id, index), ref_id, index)

    async def _capture_current_text(self, ref_id, index):
        if not self.available or self._context is None or self.page is None:
            raise RuntimeError("interactive browser session is not available")
        page = self.page
        context = self._context
        page_url = str(page.url)
        requested_url = self._requested_url
        parsed = urlparse(page_url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError("plain text capture requires an HTTP(S) page")
        origin = f"{scheme}://{parsed.netloc}"
        nonce = f"__callimachus_capture_{os.urandom(16).hex()}__"
        permission_attempted = False
        nonce_written = False
        restore_attempted = False
        copied_text = None
        capture_error = None
        cleanup_error = None
        try:
            # The persistent context owns a per-run temporary profile. Grant only
            # this page's origin and clear the temporary override in every exit.
            permission_attempted = True
            await context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=origin
            )
            await page.evaluate(_CLIPBOARD_WRITE_TEXT_SCRIPT, nonce)
            nonce_written = True
            restore_attempted = True
            focused = await page.evaluate(_PREPARE_TEXT_CAPTURE_SCRIPT)
            if focused is False:
                raise RuntimeError("browser page could not be focused for text capture")
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("ControlOrMeta+C")
            # Chrome may update the operating-system clipboard just after the
            # keyboard event. Never admit a pre-existing or unchanged clipboard.
            for _ in range(20):
                await page.wait_for_timeout(50)
                current = await page.evaluate(_CLIPBOARD_READ_TEXT_SCRIPT)
                if current != nonce:
                    copied_text = current
                    break
        except Exception as exc:
            capture_error = exc
        finally:
            if restore_attempted:
                try:
                    await page.evaluate(_RESTORE_TEXT_CAPTURE_SCRIPT)
                except Exception as exc:
                    cleanup_error = exc
            if nonce_written and copied_text is None:
                try:
                    # Remove only the nonce we installed if copy never replaced it.
                    await page.evaluate(_CLEAR_TEXT_CAPTURE_NONCE_SCRIPT, nonce)
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
            if permission_attempted:
                try:
                    await context.clear_permissions()
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
        if capture_error is not None:
            raise RuntimeError("interactive browser clipboard capture failed") from capture_error
        if cleanup_error is not None:
            raise RuntimeError("interactive browser text capture could not restore browser state") from cleanup_error
        if not isinstance(copied_text, str) or not copied_text.strip():
            return None
        try:
            # Requests are bound to the page that received the toolbar click.
            # A navigation or a newly selected tab during copy invalidates this
            # artifact rather than attaching its text to a different URL.
            if self.page is not page or str(page.url) != page_url:
                return None
        except Exception:
            return None
        body = copied_text.encode("utf-8")
        return {
            "ref_id": ref_id,
            "capture_index": index,
            "artifact_path": _write_artifact(self.artifacts_dir, ref_id, index, ".txt", body),
            "url": page_url,
            "requested_url": requested_url,
            "status": 200,
            "content_type": "text/plain; charset=utf-8",
            "body": body,
        }

    def capture_current_pdf(self, ref_id: str, index: int):
        if self.login_only:
            raise RuntimeError("capture is disabled for a login-only browser session")
        return self._call_capture(self._capture_current_pdf(ref_id, index), ref_id, index)

    async def _frame_is_visible(self, page, frame) -> bool:
        main_frame = getattr(page, "main_frame", None)
        current = frame
        while current is not main_frame:
            parent = getattr(current, "parent_frame", None)
            if parent is None:
                return False
            try:
                element = await current.frame_element()
                if not await element.is_visible():
                    return False
            except Exception:
                return False
            current = parent
        return True

    async def _discover_pdf_targets(self, page) -> list[dict]:
        frames = list(getattr(page, "frames", ()) or ())
        main_frame = getattr(page, "main_frame", None)
        if not frames and main_frame is not None:
            frames = [main_frame]
        targets: dict[str, dict] = {}
        for frame in frames:
            if not await self._frame_is_visible(page, frame):
                continue
            try:
                sources = await frame.evaluate(_PDF_VIEWER_TARGETS_SCRIPT)
            except Exception:
                continue
            if not isinstance(sources, list):
                continue
            frame_url = str(getattr(frame, "url", "") or "")
            for source in sources:
                if not isinstance(source, dict):
                    continue
                target_url = _normalize_pdf_target(source.get("url"), frame_url)
                if target_url is None:
                    continue
                targets.setdefault(target_url, {
                    "url": target_url,
                    "frame": frame,
                    "frame_url": frame_url,
                    "kind": source.get("kind"),
                })

        # Native Chrome's PDF document may not expose a scriptable DOM. Its
        # actual HTTP URL is still an explicit current-document target.
        page_url = str(getattr(page, "url", "") or "")
        normalized_page_url = _normalize_pdf_target(page_url, page_url)
        if normalized_page_url is not None and re.search(
            r"\.pdf$", urlsplit(normalized_page_url).path, flags=re.I
        ):
            targets.setdefault(normalized_page_url, {
                "url": normalized_page_url,
                "frame": main_frame,
                "frame_url": str(getattr(main_frame, "url", page_url) or page_url),
                "kind": "document",
            })
        if len(targets) > 1:
            raise RuntimeError(
                "multiple visible PDF viewer targets found; select one document before capture"
            )
        return list(targets.values())

    async def _read_pdf_target(self, page, target: dict, page_url: str) -> tuple[bytes, str] | None:
        target_url = target["url"]
        if target_url.startswith("blob:"):
            try:
                values = await target["frame"].evaluate(_READ_BLOB_PDF_SCRIPT, target_url)
                body = bytes(values) if isinstance(values, list) else b""
            except Exception:
                return None
            return (body, target_url) if body.startswith(b"%PDF") else None

        for owner_page, response in reversed(self._pdf_responses):
            if owner_page is not page:
                continue
            response_url = _normalize_pdf_target(
                getattr(response, "url", None), target.get("frame_url", page_url)
            )
            headers = {
                str(name).lower(): str(value)
                for name, value in (getattr(response, "headers", {}) or {}).items()
            }
            if response_url != target_url or getattr(response, "status", None) != 200:
                continue
            if "content-range" in headers:
                continue
            try:
                body = await response.body()
            except Exception:
                continue
            if isinstance(body, bytes) and body.startswith(b"%PDF"):
                return body, str(getattr(response, "url", target_url) or target_url)

        context = self._context
        playwright_request = getattr(self._playwright, "request", None)
        new_context = getattr(playwright_request, "new_context", None)
        if context is None or not callable(new_context):
            return None
        referer = page_url
        if urlparse(referer).scheme.lower() not in {"http", "https"}:
            referer = str(self._requested_url or "")
        if urlparse(referer).scheme.lower() not in {"http", "https"}:
            return None
        response = None
        request = None
        cookies_before = None
        cookies_after = None
        cookie_sync_error = None
        cancelled = False
        body = None
        acquired_url = target_url
        try:
            # A private APIRequestContext makes its pending Node-side transfer
            # disposable without closing Chrome or its shared request context.
            cookies_before = await context.cookies()
            user_agent = await page.evaluate("() => navigator.userAgent")
            if not isinstance(user_agent, str) or not user_agent:
                raise RuntimeError("browser user agent is unavailable for PDF capture")
            request = await new_context(
                storage_state={"cookies": cookies_before, "origins": []},
                user_agent=user_agent,
            )
            response = await request.get(
                target_url,
                headers={"Referer": referer},
                timeout=_PDF_CAPTURE_TIMEOUT_MS,
                fail_on_status_code=False,
            )
            headers = {
                str(name).lower(): str(value)
                for name, value in (getattr(response, "headers", {}) or {}).items()
            }
            if getattr(response, "status", None) != 200 or "content-range" in headers:
                return None
            body = await response.body()
            acquired_url = str(getattr(response, "url", target_url) or target_url)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            return None
        finally:
            if request is not None:
                try:
                    state = await request.storage_state()
                    cookies_after = state.get("cookies", [])
                except Exception as exc:
                    cookie_sync_error = exc
            dispose = getattr(response, "dispose", None)
            if callable(dispose):
                try:
                    await dispose()
                except Exception:
                    pass
            if request is not None:
                try:
                    await request.dispose(
                        reason=(
                            "Guided Fetch capture cancelled"
                            if cancelled else "Guided Fetch capture finished"
                        )
                    )
                except Exception:
                    pass
            if cookies_before is not None and cookies_after is not None:
                try:
                    await self._sync_capture_cookies(
                        context, cookies_before, cookies_after
                    )
                except Exception as exc:
                    cookie_sync_error = cookie_sync_error or exc
            if cookie_sync_error is not None and not cancelled:
                raise RuntimeError(
                    "PDF capture could not preserve browser authentication cookies"
                ) from cookie_sync_error
        if not isinstance(body, bytes) or not body.startswith(b"%PDF"):
            return None
        return body, acquired_url

    async def _sync_capture_cookies(self, context, before, after) -> None:
        """Apply only this request's cookie changes to the still-open Chrome jar."""
        def identity(cookie):
            return (cookie.get("name"), cookie.get("domain"), cookie.get("path"))

        def fingerprint(cookie):
            return tuple(sorted(cookie.items()))

        before_by_key = {identity(cookie): cookie for cookie in before}
        after_by_key = {identity(cookie): cookie for cookie in after}
        current_by_key = {
            identity(cookie): cookie for cookie in await context.cookies()
        }
        for key, original in before_by_key.items():
            if key not in after_by_key and current_by_key.get(key) is not None:
                if fingerprint(current_by_key[key]) == fingerprint(original):
                    await context.clear_cookies(
                        name=key[0], domain=key[1], path=key[2]
                    )
        changed = []
        for key, updated in after_by_key.items():
            original = before_by_key.get(key)
            if original is not None and fingerprint(original) == fingerprint(updated):
                continue
            current = current_by_key.get(key)
            if (original is None and current is None) or (
                original is not None and current is not None
                and fingerprint(current) == fingerprint(original)
            ):
                changed.append(updated)
        if changed:
            await context.add_cookies(changed)

    async def _pdf_capture_unchanged(
        self, page, page_url: str, requested_url: str, target: dict,
    ) -> bool:
        if self.page is not page or self._requested_url != requested_url:
            return False
        try:
            if str(page.url) != page_url or str(target["frame"].url) != target["frame_url"]:
                return False
            current_targets = await self._discover_pdf_targets(page)
        except Exception:
            return False
        return (
            len(current_targets) == 1
            and current_targets[0]["url"] == target["url"]
            and current_targets[0]["frame"] is target["frame"]
            and current_targets[0]["frame_url"] == target["frame_url"]
        )

    async def _capture_current_pdf(self, ref_id, index):
        if not self.available or self.page is None or self._context is None:
            raise RuntimeError("interactive browser session is not available")
        page = self.page
        await page.wait_for_timeout(0)
        page_url = str(page.url)
        requested_url = self._requested_url
        targets = await self._discover_pdf_targets(page)
        if not targets:
            return None
        target = targets[0]
        acquired = await self._read_pdf_target(page, target, page_url)
        if acquired is None:
            return None
        body, acquired_url = acquired
        if not body.startswith(b"%PDF") or not await self._pdf_capture_unchanged(
            page, page_url, requested_url, target
        ):
            return None
        return {
            "ref_id": ref_id,
            "capture_index": index,
            "requested_url": requested_url,
            "artifact_path": _write_artifact(
                self.artifacts_dir, ref_id, index, ".pdf", body
            ),
            "url": acquired_url,
            "status": 200,
            "content_type": "application/pdf",
            "body": body,
        }

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            self._call(self._close())
        finally:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None and thread is not threading.current_thread():
                thread.join()

    async def _close(self):
        context, playwright = self._context, self._playwright
        self._context = self._playwright = self.page = self.profile_dir = None
        self.available = False
        self._pdf_responses.clear()
        self._downloads.clear()
        self._download_records.clear()
        self._page_listener_ids.clear()
        try:
            if context is not None:
                await context.close()
        finally:
            if playwright is not None:
                await playwright.stop()


def _captured_pdf(pdf_responses, downloads, directory: Path, ref_id: str, index: int):
    """Return PDF bytes from either a response body or a browser download event."""
    for download in reversed(downloads):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe_name(ref_id)}-{index}.pdf"
        try:
            body = _save_download(download, path)
        except Exception:
            continue
        if not body.startswith(b"%PDF"):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        matching = next(
            (response for response in reversed(pdf_responses)
             if response.url == download.url),
            None,
        )
        return {
            "artifact_path": str(path),
            "url": download.url,
            # A completed browser download is the authenticated response we
            # care about.  Do not carry a stale challenge/redirect 403 into
            # the normal pipeline or it will reject valid PDF bytes before
            # extraction and identity corroboration.
            "status": matching.status if matching is not None and matching.ok else 200,
            "content_type": (
                matching.headers.get("content-type", "application/pdf")
                if matching is not None else "application/pdf"
            ),
            "body": body,
        }
    for response in reversed(pdf_responses):
        if not response.ok:
            continue
        try:
            body = response.body()
        except Exception:
            continue
        if body.startswith(b"%PDF"):
            return {
                "artifact_path": _write_artifact(directory, ref_id, index, ".pdf", body),
                "url": response.url,
                "status": response.status,
                "content_type": response.headers.get("content-type", "application/pdf"),
                "body": body,
            }
    return None


def _challenge_free_reload(page, *, timeout: float, poll_seconds: float):
    """Obtain a fresh document response whose resulting page is challenge-free."""
    deadline = time.monotonic() + timeout
    for _attempt in range(3):
        try:
            response = page.reload(wait_until="domcontentloaded")
        except Exception:
            return None
        if response is None:
            return None
        if not _challenge_active(page):
            return response
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _wait_for_user(
            page, timeout=remaining, poll_seconds=poll_seconds
        ):
            return None
    return None


def recover(groups, run_dir: str) -> dict:
    """Recover challenge-blocked URLs, or request the caller's queue fallback.

    Returned artifacts are intentionally not interpreted here.  A failed import,
    missing browser executable, or unavailable GUI returns ``available=False`` so
    the caller can create the pre-existing manual browser queue.
    """
    timeout = _number_env(ENV_INTERACTIVE_TIMEOUT, DEFAULT_INTERACTIVE_TIMEOUT, minimum=1)
    poll_seconds = _number_env(
        ENV_INTERACTIVE_POLL_SECONDS, DEFAULT_INTERACTIVE_POLL_SECONDS, minimum=0.1
    )
    items = []
    session = InteractiveBrowserSession(run_dir)
    started = session.start()
    if not started["available"]:
        return {**started, "items": []}
    try:
        page = session.page
        page.set_default_navigation_timeout(int(min(timeout, 60) * 1000))
        for group in groups:
            requires_access = bool(group.get("requires_legitimate_access"))
            prompt = (
                "Sign in with your legitimate publisher/institutional access, then continue "
                "in this same visible window; waiting..."
                if requires_access else
                "Complete any publisher challenge in the visible window; waiting..."
            )
            print(f"[interactive fetch] Browser open for {group.get('domain')}. {prompt}", file=sys.stderr)
            for reference in group.get("references") or []:
                ref_id = reference.get("ref_id")
                recovered = []
                for index, url in enumerate(reference.get("candidate_urls") or []):
                    navigation = session.open(url)
                    page.wait_for_timeout(100)
                    pdf = session.capture_current_pdf(ref_id, index)
                    if pdf is not None:
                        recovered.append({"ref_id": ref_id, "found": True, "requested_url": url, **pdf})
                        continue
                    if navigation.get("status") is None:
                        continue
                    try:
                        initial_url, initial_html = page.url, page.content()
                    except Exception:
                        continue
                    ready = (
                        _wait_for_legitimate_access(
                            page, initial_url=initial_url, initial_html=initial_html,
                            timeout=timeout, poll_seconds=poll_seconds,
                        ) if requires_access else _wait_for_user(
                            page, timeout=timeout, poll_seconds=poll_seconds,
                        )
                    )
                    if not ready:
                        continue
                    page.wait_for_timeout(100)
                    pdf = session.capture_current_pdf(ref_id, index)
                    if pdf is not None:
                        recovered.append({"ref_id": ref_id, "found": True, "requested_url": url, **pdf})
                        continue
                    # Never reuse the initial challenge response's 403 or content type.
                    session.reset_captures()
                    final_response = _challenge_free_reload(
                        page, timeout=timeout, poll_seconds=poll_seconds
                    )
                    page.wait_for_timeout(100)
                    pdf = session.capture_current_pdf(ref_id, index)
                    if pdf is not None:
                        recovered.append({"ref_id": ref_id, "found": True, "requested_url": url, **pdf})
                        continue
                    if final_response is None or _challenge_active(page):
                        continue
                    html = session.capture_current_html(ref_id, index, response=final_response)
                    if html is not None:
                        recovered.append({"ref_id": ref_id, "found": True, "requested_url": url, **html})
                items.extend(recovered or [{"ref_id": ref_id, "found": False}])
    except Exception as exc:
        return {
            "available": False,
            "reason_code": "browser_session_error",
            "reason": f"interactive browser session unavailable: {exc}",
            "items": [],
        }
    finally:
        session.close()
    return {"available": True, "reason_code": None, "reason": None, "items": items}
