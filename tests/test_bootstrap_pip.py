# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Security regressions for the verified pip bootstrap and active pin overlay."""
from __future__ import annotations

import hashlib
import importlib.util
import ssl
from pathlib import Path
import sys

import certifi
import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload: bytes, final_url: str):
        self.payload = payload
        self.final_url = final_url
        self.headers = {"Content-Length": str(len(payload))}
        self.read_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def geturl(self):
        return self.final_url

    def read(self, size: int) -> bytes:
        self.read_count += 1
        if self.read_count > 1:
            return b""
        return self.payload


def load_bootstrap():
    return load_module(
        "callimachus_bootstrap_pip",
        ROOT / "packaging" / "bootstrap_pip.py",
    )


def test_bootstrap_download_uses_verified_https_and_exact_digest(tmp_path):
    bootstrap = load_bootstrap()
    payload = b"verified pip wheel fixture"
    response = FakeResponse(payload, bootstrap.PIP_WHEEL_URL)
    calls = []

    def opener(url, *, timeout, context):
        calls.append((url, timeout, context))
        return response

    wheel = bootstrap.download_verified_wheel(
        tmp_path / "pip.whl",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        opener=opener,
    )

    assert wheel.read_bytes() == payload
    assert calls[0][0] == bootstrap.PIP_WHEEL_URL
    assert calls[0][1] == bootstrap.DOWNLOAD_TIMEOUT_SECONDS
    assert calls[0][2].verify_mode == ssl.CERT_REQUIRED
    assert calls[0][2].check_hostname is True


def test_macos_bootstrap_uses_os_ca_file_with_verified_tls(monkeypatch, tmp_path):
    bootstrap = load_bootstrap()
    payload = b"verified pip wheel fixture"
    response = FakeResponse(payload, bootstrap.PIP_WHEEL_URL)
    verified_context = ssl.create_default_context(cafile=certifi.where())
    ca_files = []

    def create_context(*, cafile=None):
        ca_files.append(cafile)
        return verified_context

    def opener(_url, *, timeout, context):
        assert timeout == bootstrap.DOWNLOAD_TIMEOUT_SECONDS
        assert context is verified_context
        return response

    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(bootstrap.ssl, "create_default_context", create_context)
    bootstrap.download_verified_wheel(
        tmp_path / "pip.whl",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        opener=opener,
    )

    assert ca_files == ["/etc/ssl/cert.pem"]
    assert verified_context.verify_mode == ssl.CERT_REQUIRED
    assert verified_context.check_hostname is True


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_default_context_defers_lazy_ca_validation_to_handshake(
    monkeypatch, tmp_path, platform
):
    bootstrap = load_bootstrap()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(capath=str(tmp_path))
    assert context.cert_store_stats()["x509_ca"] == 0
    monkeypatch.setattr(bootstrap.sys, "platform", platform)
    monkeypatch.setattr(bootstrap.ssl, "create_default_context", lambda: context)

    assert bootstrap._bootstrap_ssl_context() is context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_bootstrap_tls_failure_never_invokes_install(monkeypatch):
    bootstrap = load_bootstrap()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    install_calls = []
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap.ssl, "create_default_context", lambda: context)

    def verifier():
        raise bootstrap.BootstrapPipError("pip needs bootstrap")

    def opener(_url, *, timeout, context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        raise ssl.SSLCertVerificationError("untrusted CA")

    with pytest.raises(ssl.SSLCertVerificationError, match="untrusted CA"):
        bootstrap.bootstrap(
            opener=opener,
            verifier=verifier,
            command_runner=lambda *args, **kwargs: install_calls.append(args),
        )

    assert install_calls == []

@pytest.mark.parametrize("failure", ["missing", "invalid", "empty"])
def test_macos_bootstrap_fails_closed_without_a_loadable_os_ca(
    monkeypatch, failure
):
    bootstrap = load_bootstrap()
    download_calls = []
    install_calls = []

    def create_context(*, cafile=None):
        assert cafile == "/etc/ssl/cert.pem"
        if failure == "missing":
            raise FileNotFoundError("system CA unavailable")
        if failure == "invalid":
            raise ssl.SSLError("invalid system CA")
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def verifier():
        raise bootstrap.BootstrapPipError("pip needs bootstrap")

    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(bootstrap.ssl, "create_default_context", create_context)
    expected_error = "no trusted CA" if failure == "empty" else "macOS system CA"
    with pytest.raises(bootstrap.BootstrapPipError, match=expected_error):
        bootstrap.bootstrap(
            opener=lambda *args, **kwargs: download_calls.append((args, kwargs)),
            command_runner=lambda *args, **kwargs: install_calls.append((args, kwargs)),
            verifier=verifier,
        )

    assert download_calls == []
    assert install_calls == []


def test_bootstrap_rejects_redirect_before_reading_payload(tmp_path):
    bootstrap = load_bootstrap()
    response = FakeResponse(b"do not read", "http://files.pythonhosted.org/pip.whl")

    with pytest.raises(bootstrap.BootstrapPipError, match="redirect"):
        bootstrap.download_verified_wheel(
            tmp_path / "pip.whl",
            expected_size=len(response.payload),
            expected_sha256=hashlib.sha256(response.payload).hexdigest(),
            opener=lambda *_args, **_kwargs: response,
        )

    assert response.read_count == 0
    assert not (tmp_path / "pip.whl").exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://files.pythonhosted.org:444/pip.whl",
        "https://user@files.pythonhosted.org/pip.whl",
    ],
)
def test_bootstrap_requires_the_exact_https_origin(url, tmp_path):
    bootstrap = load_bootstrap()
    calls = []

    with pytest.raises(bootstrap.BootstrapPipError, match="HTTPS"):
        bootstrap.download_verified_wheel(
            tmp_path / "pip.whl",
            url=url,
            opener=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

    assert calls == []
    assert not (tmp_path / "pip.whl").exists()


def test_intermediate_redirect_is_rejected_before_follow_or_install(monkeypatch):
    bootstrap = load_bootstrap()
    state = {"followed": False, "installed": False}

    class RedirectingOpener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, _url, *, timeout):
            handler = next(
                item for item in self.handlers
                if isinstance(item, bootstrap._PinnedHTTPSRedirectHandler)
            )
            handler.redirect_request(
                None, None, 302, "Found", {}, "http://files.pythonhosted.org/pip.whl"
            )
            state["followed"] = True

    def build_opener(*handlers):
        return RedirectingOpener(handlers)

    verify_calls = 0

    def verifier():
        nonlocal verify_calls
        verify_calls += 1
        raise bootstrap.BootstrapPipError("pip needs bootstrap")

    monkeypatch.setattr(bootstrap.request, "build_opener", build_opener)
    with pytest.raises(bootstrap.BootstrapPipError, match="redirect"):
        bootstrap.bootstrap(
            command_runner=lambda *_args, **_kwargs: state.__setitem__("installed", True),
            verifier=verifier,
        )

    assert verify_calls == 1
    assert state == {"followed": False, "installed": False}


def test_bad_wheel_hash_never_invokes_pip_install():
    bootstrap = load_bootstrap()
    payload = b"wrong wheel bytes"
    response = FakeResponse(payload, bootstrap.PIP_WHEEL_URL)
    verify_calls = 0
    install_calls = []

    def verifier():
        nonlocal verify_calls
        verify_calls += 1
        raise bootstrap.BootstrapPipError("pip needs bootstrap")

    with pytest.raises(bootstrap.BootstrapPipError, match="SHA-256"):
        bootstrap.bootstrap(
            opener=lambda *_args, **_kwargs: response,
            command_runner=lambda *args, **kwargs: install_calls.append((args, kwargs)),
            verifier=verifier,
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(b"different wheel bytes").hexdigest(),
        )

    assert verify_calls == 1
    assert install_calls == []


def test_verified_wheel_is_installed_offline_then_versions_are_checked(tmp_path):
    bootstrap = load_bootstrap()
    payload = b"verified pip wheel fixture"
    response = FakeResponse(payload, bootstrap.PIP_WHEEL_URL)
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("verified CA fixture", encoding="ascii")
    verify_calls = 0
    install_calls = []

    def verifier():
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            raise bootstrap.BootstrapPipError("pip needs bootstrap")
        return ("26.2.2", "2026.10.19", bundle)

    def runner(command, *, check):
        install_calls.append((command, check))
        assert Path(command[-1]).read_bytes() == payload

    result = bootstrap.bootstrap(
        opener=lambda *_args, **_kwargs: response,
        command_runner=runner,
        verifier=verifier,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    command, check = install_calls[0]
    assert check is True
    assert command[:3] == [bootstrap.sys.executable, "-m", "pip"]
    assert "--no-index" in command
    assert "--no-deps" in command
    assert "--no-cache-dir" in command
    assert "--trusted-host" not in command
    assert result == ("26.2.2", "2026.10.19", bundle)


def test_postcheck_requires_patched_vendored_certifi(tmp_path, monkeypatch):
    bootstrap = load_bootstrap()
    vendor = tmp_path / "pip" / "_vendor" / "certifi"
    vendor.mkdir(parents=True)
    (vendor / "__init__.py").write_text('__version__ = "2026.10.19"\n', encoding="utf-8")
    bundle = vendor / "cacert.pem"
    bundle.write_text("CA bundle", encoding="ascii")

    class Distribution:
        version = "26.2.2"

        @staticmethod
        def locate_file(relative):
            return tmp_path / relative

    monkeypatch.setattr(bootstrap.importlib.metadata, "distribution", lambda _name: Distribution())
    assert bootstrap.verify_installed_pip() == ("26.2.2", "2026.10.19", bundle.resolve())

    (vendor / "__init__.py").write_text('__version__ = "2026.10.17"\n', encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapPipError, match="older than"):
        bootstrap.verify_installed_pip()




def test_macos_aqt_environment_installs_audit_import_dependency_explicitly():
    script = (ROOT / "packaging" / "prepare_macos_bindings.sh").read_text(encoding="utf-8")
    install_start = script.index('"$AQT_VENV/bin/python" -m pip install --only-binary=:all:')
    pip_check_start = script.index('"$AQT_VENV/bin/python" -m pip check', install_start)
    audit_start = script.index('"$AQT_VENV/bin/python" "$ROOT/packaging/dependency_audit.py"', install_start)
    install_command = script[install_start:pip_check_start]
    audit_source = (ROOT / "packaging" / "dependency_audit.py").read_text(encoding="utf-8")

    assert '"$AQT_WHEEL"' in install_command
    assert "packaging==26.3" in install_command.split()
    assert install_start < audit_start
    assert "from packaging.version import InvalidVersion, Version" in audit_source


def test_final_requirements_overlay_secure_constraints_without_mutating_baseline():
    recipe = load_module(
        "callimachus_prepare_final_requirements",
        ROOT / "packaging" / "store" / "native-recipes" / "prepare-final-requirements.py",
    )
    baseline = {
        "certifi": "2026.6.17",
        "setuptools": "84.0.0",
        "Brotli": "1.2.0",
        "Pillow": "12.3.0",
        "PySide6": "6.11.2",
        "other-runtime-package": "1.2.3",
    }
    overrides = {
        "certifi": "2026.10.19",
        "setuptools": "84.1.0",
        "pip": "26.2.2",
        "wheel": "0.46.3",
        "Brotli": "1.2.1",
        "Pillow": "13.0.0",
        "PySide6": "6.12.0",
    }

    lines = recipe.render_requirements(baseline, set(), overrides)

    assert "certifi==2026.10.19" in lines
    assert "setuptools==84.1.0" in lines
    assert "Brotli==1.2.1" in lines
    assert "Pillow==13.0.0" in lines
    assert "PySide6==6.11.2" in lines
    assert "other-runtime-package==1.2.3" in lines
    assert not any(line.startswith(("pip==", "wheel==")) for line in lines)
    assert baseline["certifi"] == "2026.6.17"
    assert baseline["setuptools"] == "84.0.0"
    assert baseline["Brotli"] == "1.2.0"
    assert baseline["Pillow"] == "12.3.0"


def test_staged_recipe_reads_active_constraints_from_project_root():
    recipe = load_module(
        "callimachus_prepare_final_requirements_staged",
        ROOT / "packaging" / "store" / "native-recipes" / "prepare-final-requirements.py",
    )
    expected = ROOT / "packaging" / "release-constraints.txt"
    for recipe_root in (
        ROOT / "packaging" / "store" / "native-recipes",
        ROOT / "build" / "desktop" / "native-14.40",
    ):
        assert recipe.release_constraints_path(recipe_root) == expected

    active = recipe.validated_active_constraints(expected)
    assert active == {
        "certifi": "2026.10.19",
        "pip": "26.2.2",
        "setuptools": "84.1.0",
        "wheel": "0.46.3",
        "brotli": "1.2.1",
        "pillow": "13.0.0",
    }


def test_final_requirements_refuse_historical_security_pins(tmp_path):
    recipe = load_module(
        "callimachus_prepare_final_requirements_old_pins",
        ROOT / "packaging" / "store" / "native-recipes" / "prepare-final-requirements.py",
    )
    stale = tmp_path / "release-constraints.txt"
    stale.write_text(
        "certifi==2026.6.17\npip==26.2.1\nsetuptools==84.0.0\nwheel==0.45.1\nBrotli==1.2.0\nPillow==12.3.0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="active security minimums"):
        recipe.validated_active_constraints(stale)
