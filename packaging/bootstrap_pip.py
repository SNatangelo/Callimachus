#!/usr/bin/env python3
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bootstrap a trusted pip before it performs any network installation."""
from __future__ import annotations

import hashlib
import importlib.metadata
import re
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib import request


PIP_VERSION = "26.2.2"
PIP_VENDOR_CERTIFI_MINIMUM = "2026.10.18"
PIP_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/3f/06/"
    "3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/"
    "pip-26.2.2-py3-none-any.whl"
)
PIP_WHEEL_FILENAME = "pip-26.2.2-py3-none-any.whl"
PIP_WHEEL_SIZE = 1_819_850
PIP_WHEEL_SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
PIP_WHEEL_HOST = "files.pythonhosted.org"
DOWNLOAD_TIMEOUT_SECONDS = 60
CHUNK_SIZE = 1024 * 1024
MACOS_SYSTEM_CA_FILE = "/etc/ssl/cert.pem"


def _is_pinned_https_origin(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == PIP_WHEEL_HOST
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


class BootstrapPipError(RuntimeError):
    """Raised when the pinned pip bootstrap cannot be verified."""


def _bootstrap_ssl_context() -> ssl.SSLContext:
    """Use macOS's OS trust file before Python.org certifi is provisioned."""
    try:
        if sys.platform == "darwin":
            context = ssl.create_default_context(cafile=str(MACOS_SYSTEM_CA_FILE))
        else:
            context = ssl.create_default_context()
    except (OSError, ssl.SSLError, ValueError) as exc:
        if sys.platform == "darwin":
            raise BootstrapPipError(
                f"could not load the macOS system CA file {MACOS_SYSTEM_CA_FILE}"
            ) from exc
        raise BootstrapPipError("could not create the verified TLS context") from exc

    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise BootstrapPipError("pip bootstrap requires TLS certificate and hostname verification")
    if context.cert_store_stats().get("x509_ca", 0) < 1:
        raise BootstrapPipError("bootstrap TLS context has no trusted CA certificates")
    return context


class _PinnedHTTPSRedirectHandler(request.HTTPRedirectHandler):
    """Reject a redirect before urllib can follow it off the pinned HTTPS host."""

    handler_order = request.HTTPRedirectHandler.handler_order - 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_pinned_https_origin(newurl):
            raise BootstrapPipError("pip wheel redirect left the verified HTTPS origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_verified_url(url: str, *, timeout: int, context):
    opener = request.build_opener(
        _PinnedHTTPSRedirectHandler(),
        request.HTTPSHandler(context=context),
    )
    return opener.open(url, timeout=timeout)


def _version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+)*", value):
        raise BootstrapPipError(f"unexpected package version format: {value!r}")
    return tuple(int(part) for part in value.split("."))


def download_verified_wheel(
    destination: Path,
    *,
    url: str = PIP_WHEEL_URL,
    expected_size: int = PIP_WHEEL_SIZE,
    expected_sha256: str = PIP_WHEEL_SHA256,
    opener=None,
) -> Path:
    """Fetch the pinned wheel with verified TLS, origin, size and digest."""
    if not _is_pinned_https_origin(url):
        raise BootstrapPipError("pip wheel URL must use HTTPS on files.pythonhosted.org")

    open_url = opener or _open_verified_url
    context = _bootstrap_ssl_context()
    partial = destination.with_name(destination.name + ".partial")
    digest = hashlib.sha256()
    total = 0
    try:
        with open_url(url, timeout=DOWNLOAD_TIMEOUT_SECONDS, context=context) as response:
            if not _is_pinned_https_origin(response.geturl()):
                raise BootstrapPipError("pip wheel redirect left the verified HTTPS origin")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    announced_size = int(content_length)
                except ValueError as exc:
                    raise BootstrapPipError("pip wheel has an invalid Content-Length") from exc
                if announced_size != expected_size:
                    raise BootstrapPipError("pip wheel Content-Length does not match the pin")

            with partial.open("xb") as output:
                while True:
                    block = response.read(CHUNK_SIZE)
                    if not block:
                        break
                    total += len(block)
                    if total > expected_size:
                        raise BootstrapPipError("pip wheel exceeds the pinned size")
                    output.write(block)
                    digest.update(block)

        if total != expected_size:
            raise BootstrapPipError(
                f"pip wheel size mismatch: expected {expected_size}, received {total}"
            )
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise BootstrapPipError("pip wheel SHA-256 does not match the pin")
        partial.replace(destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _installed_pip_state() -> tuple[str, str, Path]:
    """Read pip's installed metadata and vendored CA files without importing pip."""
    try:
        distribution = importlib.metadata.distribution("pip")
        actual_pip = distribution.version
        vendor_init = Path(
            distribution.locate_file("pip/_vendor/certifi/__init__.py")
        )
        match = re.search(
            r"(?m)^__version__\s*=\s*['\"]([^'\"]+)['\"]\s*$",
            vendor_init.read_text(encoding="utf-8"),
        )
        if match is None:
            raise BootstrapPipError("pip vendored certifi has no version declaration")
        vendor_certifi = match.group(1)
        bundle = Path(
            distribution.locate_file("pip/_vendor/certifi/cacert.pem")
        ).resolve(strict=True)
    except Exception as exc:
        if isinstance(exc, BootstrapPipError):
            raise
        raise BootstrapPipError("could not verify installed pip and its vendored CA bundle") from exc

    if not bundle.is_file() or bundle.stat().st_size == 0:
        raise BootstrapPipError("pip's vendored CA bundle is missing or empty")
    return actual_pip, vendor_certifi, bundle


def verify_installed_pip() -> tuple[str, str, Path]:
    """Require the pinned pip and its patched vendored CA bundle."""
    actual_pip, vendor_certifi, bundle = _installed_pip_state()
    if actual_pip != PIP_VERSION:
        raise BootstrapPipError(f"expected pip {PIP_VERSION}, found {actual_pip}")
    if _version_tuple(vendor_certifi) < _version_tuple(PIP_VENDOR_CERTIFI_MINIMUM):
        raise BootstrapPipError(
            "pip's vendored certifi is older than "
            f"{PIP_VENDOR_CERTIFI_MINIMUM}: {vendor_certifi}"
        )
    return actual_pip, vendor_certifi, bundle


def bootstrap(
    *,
    opener=None,
    command_runner=None,
    verifier=None,
    expected_size: int = PIP_WHEEL_SIZE,
    expected_sha256: str = PIP_WHEEL_SHA256,
) -> tuple[str, str, Path]:
    """Install the hash-verified local wheel offline, then verify its CA data."""
    run = command_runner or subprocess.run
    verify = verifier or verify_installed_pip
    try:
        return verify()
    except BootstrapPipError:
        pass
    with tempfile.TemporaryDirectory(prefix="callimachus-pip-bootstrap-") as temporary:
        wheel = Path(temporary) / PIP_WHEEL_FILENAME
        download_verified_wheel(
            wheel,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            opener=opener,
        )
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "--no-input",
                "install",
                "--upgrade",
                "--force-reinstall",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                str(wheel),
            ],
            check=True,
        )
    result = verify()
    if result[0] != PIP_VERSION:
        raise BootstrapPipError(f"expected pip {PIP_VERSION}, found {result[0]}")
    if _version_tuple(result[1]) < _version_tuple(PIP_VENDOR_CERTIFI_MINIMUM):
        raise BootstrapPipError(
            "pip's vendored certifi is older than "
            f"{PIP_VENDOR_CERTIFI_MINIMUM}: {result[1]}"
        )
    return result


def main() -> int:
    pip_version, certifi_version, bundle = bootstrap()
    print(
        f"Verified pip {pip_version} with vendored certifi {certifi_version} "
        f"({bundle.stat().st_size} CA bytes).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
