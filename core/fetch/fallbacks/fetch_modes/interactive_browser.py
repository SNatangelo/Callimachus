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
from pathlib import Path
from urllib.parse import urlparse

from core.fetch.extraction import fetch_html
from core.fetch.fallbacks.fetch_modes import browser_challenge

NAME = "interactive_browser"
# ``interactive`` remains an alias for runs created while this mode was being
# introduced.  The explicit name makes its deliberately narrow scope visible
# in the startup summary.
MODE_NAME = "interactive_challenge"
ALIASES = ("interactive", "playwright", "interactive_browser")
ENV_INTERACTIVE_TIMEOUT = "CITATION_VERIFIER_FETCH_INTERACTIVE_TIMEOUT"
ENV_INTERACTIVE_POLL_SECONDS = "CITATION_VERIFIER_FETCH_INTERACTIVE_POLL_SECONDS"
DEFAULT_INTERACTIVE_TIMEOUT = 300
DEFAULT_INTERACTIVE_POLL_SECONDS = 2.0
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
    const x = Number.isFinite(host.__captureX) ? host.__captureX : left + width - panelWidth - gap;
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


def _capture_toolbar_script(*, dark: bool = False) -> str:
    return _CAPTURE_TOOLBAR_SCRIPT.replace('__CALLIMACHUS_THEME__', 'dark' if dark else 'light')
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

    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
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
            self.page.evaluate(_capture_toolbar_script())

    @staticmethod
    def _install_toolbar_on_page(page) -> None:
        try:
            page.evaluate(_capture_toolbar_script())
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
            )
            self._context.add_init_script(script=_capture_toolbar_script())
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self.page.on(
                "response",
                lambda response: self._pdf_responses.append(response)
                if "application/pdf" in response.headers.get("content-type", "").lower()
                else None,
            )
            self.page.on("download", lambda download: self._downloads.append(download))
            self._context.on("page", self._install_toolbar_on_page)
            self._install_capture_toolbar()
        except Exception as exc:
            self.close()
            return self._unavailable(
                "google_chrome_unavailable",
                f"Google Chrome unavailable: {exc}; Chrome is an external prerequisite",
            )
        self.available = True
        self.reason = None
        self.reason_code = None
        return {"available": True, "reason_code": None, "reason": None}

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
        """Close runtime handles and remove the credential-bearing temporary profile."""
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
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)


class AsyncInteractiveBrowserSession:
    """Thread-safe facade for the Guided Fetch browser only.

    The async Playwright objects live exclusively on the private event-loop
    thread.  Qt invokes this facade synchronously from its worker thread, so
    no Playwright dispatcher or asyncio loop crosses a Qt callback boundary.
    """

    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        self.profile_dir: Path | None = None
        self.artifacts_dir = self.run_dir / "browser_interactive_artifacts"
        self.available = False
        self.reason = None
        self.reason_code = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._playwright = self._context = self.page = None
        self._pdf_responses = []
        self._downloads = []
        self._download_records = {}
        self._next_download_token = 1
        self._requested_url = self._last_response = None
        self._stale = False
        self._toolbar_failures: dict[int, int] = {}
        self._toolbar_reported: set[int] = set()
        self._dark_theme = False

    def set_theme(self, dark: bool) -> None:
        """Apply the GUI theme to current pages and future navigations."""
        self._dark_theme = bool(dark)
        if self.available:
            self._call(self._apply_theme())

    async def _apply_theme(self) -> None:
        if self._context is None:
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
            if not self.run_dir.is_dir():
                raise FileNotFoundError("run directory is unavailable")
            self.profile_dir = Path(tempfile.mkdtemp(prefix="citation-verifier-chrome-", dir=self.run_dir))
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
            return self._unavailable("browser_launch_failed", f"Google Chrome could not be started: {exc}")

    async def _start(self, async_playwright) -> dict:
        self._playwright = await async_playwright().start()
        return await self._launch_context()

    async def _launch_context(self) -> dict:
        """Launch Chrome against the already-created temporary profile."""
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir), channel="chrome", headless=False, accept_downloads=True,
            chromium_sandbox=True,
        )
        await self._context.add_init_script(script=_capture_toolbar_script(dark=self._dark_theme))
        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self.page.on("response", self._record_pdf_response)
        self.page.on("download", self._record_download)
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

    async def _close_visible_context(self) -> None:
        context = self._context
        self._context = None
        self.page = None
        self.available = False
        self._stale = True
        self._pdf_responses.clear()
        self._downloads.clear()
        self._download_records.clear()
        self._toolbar_failures.clear()
        self._toolbar_reported.clear()
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

    async def _download_body(self, record):
        body = record.get("body")
        if body is not None:
            return body
        path = await record["download"].path()
        body = Path(path).read_bytes()
        record["body"] = body
        return body

    def drain_download_notices(self) -> list[dict]:
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
        self._call(self._discard_download(token))

    async def _discard_download(self, token: int) -> None:
        for record in self._download_records.values():
            if record.get("token") == token:
                record["discarded"] = True
                return

    def capture_download_pdf(self, ref_id: str, index: int, token: int):
        return self._call(self._capture_download_pdf(ref_id, index, token))

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

    def _record_pdf_response(self, response):
        if "application/pdf" in response.headers.get("content-type", "").lower():
            self._pdf_responses.append(response)

    def _install_toolbar_on_page(self, page):
        asyncio.create_task(self._install_toolbar(page))

    async def _install_toolbar(self, page):
        try:
            await page.evaluate(_capture_toolbar_script(dark=self._dark_theme))
            return bool(await page.evaluate(_CAPTURE_TOOLBAR_VISIBLE_SCRIPT))
        except Exception:
            return False

    async def _install_capture_toolbar(self):
        if self.page is not None:
            await self._install_toolbar(self.page)

    def open(self, url: str):
        return self._call(self._open(url))

    @property
    def stale(self) -> bool:
        """Whether the visible browser was closed outside the application."""
        return self._stale

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
        captured = []
        toolbar_warning = False
        for page in reversed(self._context.pages):
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
                captured.extend(item for item in requests if item in {"html", "pdf"})
                if requests:
                    self.page = page
        if toolbar_warning and not captured:
            raise RuntimeError(
                "Capture HTML/PDF controls are not visible on this browser page. "
                "Callimachus will keep trying to show them; you can still choose a local file."
            )
        return captured

    def capture_current_html(self, ref_id: str, index: int):
        return self._call(self._capture_current_html(ref_id, index))

    async def _capture_current_html(self, ref_id, index):
        html = await _capture_rendered_html_async(self.page)
        if fetch_html.is_challenge_html(html):
            return None
        return {"ref_id": ref_id, "capture_index": index,
                "artifact_path": _write_artifact(self.artifacts_dir, ref_id, index, ".html", html.encode("utf-8")),
                "url": self.page.url, "requested_url": self._requested_url, "status": 200,
                "content_type": "text/html", "body": html.encode("utf-8")}

    def capture_current_pdf(self, ref_id: str, index: int):
        return self._call(self._capture_current_pdf(ref_id, index))

    async def _capture_current_pdf(self, ref_id, index):
        await self.page.wait_for_timeout(0)
        for download in reversed(self._downloads):
            record = self._download_records.get(id(download))
            if record is not None and (
                record["noticed"] or record["claimed"] or record["discarded"]
            ):
                continue
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            path = self.artifacts_dir / f"{_safe_name(ref_id)}-{index}.pdf"
            try:
                await download.save_as(str(path))
                body = path.read_bytes()
            except Exception:
                continue
            if body.startswith(b"%PDF"):
                if record is not None:
                    record["claimed"] = True
                return {"ref_id": ref_id, "capture_index": index, "requested_url": self._requested_url,
                        "artifact_path": str(path), "url": download.url, "status": 200,
                        "content_type": "application/pdf", "body": body}
        for response in reversed(self._pdf_responses):
            if not response.ok:
                continue
            try:
                body = await response.body()
            except Exception:
                continue
            if body.startswith(b"%PDF"):
                return {"ref_id": ref_id, "capture_index": index, "requested_url": self._requested_url,
                        "artifact_path": _write_artifact(self.artifacts_dir, ref_id, index, ".pdf", body),
                        "url": response.url, "status": response.status,
                        "content_type": response.headers.get("content-type", "application/pdf"), "body": body}
        return None

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
        context, playwright, profile = self._context, self._playwright, self.profile_dir
        self._context = self._playwright = self.page = self.profile_dir = None
        self.available = False
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if playwright is not None:
                    await playwright.stop()
            finally:
                if profile is not None:
                    shutil.rmtree(profile, ignore_errors=True)


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
