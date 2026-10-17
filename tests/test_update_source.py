"""TC-02: where releases come from. Production stays GitHub only; a test release source is injected
by the harness (scripts/update_source_adapter.py), never through a switch of TOW's own.

Production: release discovery asks GitHub's API through the public-only transport, without proxies
or redirects; the updater downloads from GitHub and its download hosts over HTTPS, whatever the
environment says; malformed authorities, other ports, credentials, plain http, escaped and
non-loopback look-alikes are refused. The adapter: one exact loopback origin, at the two
boundaries only, in a throwaway install.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_update import SCRIPT, updater

from tow import releases
from tow.net_guard import PublicOnlyTransport

ROOT = SCRIPT.parents[1]


def _adapter():
    spec = importlib.util.spec_from_file_location(
        "tow_update_source_adapter_under_test", SCRIPT.with_name("update_source_adapter.py")
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter = _adapter()


# --- production: unchanged ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "variable", ["TOW_UPDATE_GITHUB", "TOW_INSTALL_URL", "TOW_RELEASES_URL", "GITHUB_SERVER_URL", "HTTPS_PROXY"]
)
def test_no_variable_moves_the_updaters_source(tmp_path, monkeypatch, variable):
    monkeypatch.setenv(variable, "http://127.0.0.1:18999")
    machine = updater.System(tmp_path / "TOW" / "app")
    assert (machine.github, machine.repo) == ("https://github.com", "d0j/tow")
    assert updater.GITHUB == "https://github.com"


@pytest.mark.parametrize(
    "target",
    [
        "http://github.com/d0j/tow/archive/x.tar.gz",  # plain http
        "https://github.com:444/d0j/tow/x",  # another port
        "https://github.com:abc/x",  # a malformed port
        "https://user@github.com/x",  # credentials
        "https://user:pw@objects.githubusercontent.com/x",
        "https://github.com@evil.example/x",  # an authority that only starts like GitHub's
        "https://objects.githubusercontent.com.evil.example/x",
        "https://github%2ecom/x",  # an escaped dot
        "https://GITHUB.COM.evil.example/x",
        "https://127.0.0.1/x",  # loopback and private look-alikes
        "https://127.0.0.1:18999/x",
        "http://127.0.0.1:18999/x",
        "https://[::1]/x",
        "https://0x7f.0.0.1/x",
        "https://localhost/x",
        "ftp://github.com/x",
        "file:///C:/x",
        "//evil.example/x",
    ],
)
def test_a_download_is_redirected_to_github_only(target):
    assert updater.allowed_redirect("https://github.com", target) is False


@pytest.mark.parametrize(
    "target",
    [
        "https://github.com/d0j/tow/releases/download/v1.33.1/SHA256SUMS",
        "https://codeload.github.com/d0j/tow/tar.gz/refs/tags/v1.33.1",
        "https://objects.githubusercontent.com:443/x",
        "https://release-assets.githubusercontent.com/x",
    ],
)
def test_githubs_own_download_hosts_stay_allowed(target):
    assert updater.allowed_redirect("https://github.com", target) is True


def test_release_discovery_asks_only_githubs_api_without_proxies_or_redirects(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **options):
            seen.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def stream(self, method, url):
            seen["url"] = url
            raise httpx.ConnectError("no network in tests")

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(releases.httpx, "Client", Client)
    with pytest.raises(httpx.ConnectError):
        releases.published_version("1.33.1")
    assert seen["url"] == "https://api.github.com/repos/d0j/tow/releases/tags/v1.33.1"
    assert isinstance(seen["transport"], PublicOnlyTransport)  # loopback and private answers refused
    assert seen["trust_env"] is False  # no proxy, no .netrc
    assert seen["follow_redirects"] is False
    assert releases.LATEST_URL == "https://api.github.com/repos/d0j/tow/releases/latest"


@pytest.mark.parametrize(
    "version", ["1.33", "1.33.1/../x", "v1.33.1?x=1", "1.33.1#x", "01.33.1", "1.33.1 ", "http://127.0.0.1:9/1.33.1", ""]
)
def test_a_malformed_version_never_reaches_a_url(monkeypatch, version):
    monkeypatch.setattr(releases, "_fetch_release", lambda *_a, **_k: pytest.fail("no request"))
    with pytest.raises(ValueError, match="invalid version"):
        releases.published_version(version)


def test_tow_never_imports_the_test_source():
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "update_source_adapter" not in text, path
        assert adapter.MODULE not in text, path
    assert "update_source_adapter" not in SCRIPT.read_text(encoding="utf-8")


# --- the harness's adapter -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.2:8000",
        "http://localhost:8000",
        "https://127.0.0.1:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:8000/x",
        "http://127.0.0.1:8000?x",
        "http://u@127.0.0.1:8000",
        "http://127.0.0.1:80000",
        "http://127.0.0.1:8000#x",
        "http://[::1]:8000",
        "http://0x7f000001:8000",
        "http://127.0.0.1.evil.example:8000",
    ],
)
def test_the_adapter_takes_one_exact_loopback_origin(origin):
    with pytest.raises(ValueError, match="not a loopback origin"):
        adapter.loopback_origin(origin)
    assert adapter.loopback_origin("http://127.0.0.1:18999") == "http://127.0.0.1:18999"


def test_the_adapted_updater_downloads_from_the_origin_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:3128")
    text = adapter.adapt_updater(SCRIPT.read_text(encoding="utf-8"), "http://127.0.0.1:18999")
    assert adapter.adapt_updater(text, "http://127.0.0.1:18999") == text  # once
    copy = tmp_path / "update.py"
    copy.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("tow_adapted_updater", copy)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    machine = module.System(tmp_path / "TOW" / "app")
    assert (machine.github, machine.repo) == ("http://127.0.0.1:18999", "d0j/tow")
    # its redirects: within that origin, else GitHub's HTTPS download hosts (the updater's own rule)
    assert module.allowed_redirect(machine.github, "http://127.0.0.1:18999/d0j/tow/x")
    assert not module.allowed_redirect(machine.github, "http://127.0.0.1:18998/d0j/tow/x")
    assert not module.allowed_redirect(machine.github, "http://evil.example/x")
    # no proxy between the worker and 127.0.0.1
    proxies = [h for h in machine._opener().handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert [handler.proxies for handler in proxies if handler.proxies] == []  # HTTP_PROXY is not used


def test_the_adapted_updater_is_adapted_before_it_runs_as_a_script():
    # TC-16: the entry files run update.py itself, which copies itself into its own process; the
    # suffix after `if __name__ == "__main__":` came too late there, and the update went to GitHub.
    text = adapter.adapt_updater(SCRIPT.read_text(encoding="utf-8"), "http://127.0.0.1:18999")
    guard = 'if __name__ == "__main__":'
    assert text.index(adapter.MARKER) < text.rindex(guard)
    assert text.rstrip().endswith("sys.exit(main())")
    assert adapter.adapt_updater("x = 1\n", "http://127.0.0.1:18999").startswith("x = 1\n")  # no guard: appended


def test_discovery_answers_the_test_release_only():
    fake = SimpleNamespace(version_parts=releases.version_parts)
    adapter.patch_releases(fake, "1.99.0")
    assert fake.published_version("v1.99.0") == "1.99.0"
    assert fake._fetch_latest() == "1.99.0"
    for other in ("1.33.1", "1.99.0/../x", "latest"):
        with pytest.raises(ValueError, match="not the test release"):
            fake.published_version(other)


def test_the_handoff_runs_the_adapted_copy(tmp_path):
    job = tmp_path / "web-update" / "abc"
    job.mkdir(parents=True)
    (job / "update.py").write_text("GITHUB = 'x'\n", encoding="utf-8")
    calls = []

    class Backend:
        def spawn_detached(self, argv, **options):
            calls.append((argv, options))

    adapter.patch_handoff(Backend, "http://127.0.0.1:18999")
    Backend().spawn_detached(["python", "-I", str(job / "worker.py"), "--handoff", "app"], hidden=True)
    assert adapter.MARKER in (job / "update.py").read_text(encoding="utf-8")
    assert calls[0][1] == {"hidden": True}
    Backend().spawn_detached(["python", "other.py"])  # any other start: untouched
    assert len(calls) == 2


def test_install_writes_only_into_the_given_environment(tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    written = adapter.install(site, "http://127.0.0.1:18999", "v1.99.0")
    assert sorted(path.name for path in written) == [f"{adapter.MODULE}.pth", f"{adapter.MODULE}.py"]
    assert sorted(path.name for path in site.iterdir()) == sorted(path.name for path in written)
    assert (site / f"{adapter.MODULE}.pth").read_text(encoding="utf-8") == f"import {adapter.MODULE}\n"
    with pytest.raises(ValueError, match="not a loopback origin"):
        adapter.install(site, "http://example.com:80", "v1.99.0")


@pytest.mark.allow_system  # a loopback server in this test, asked by the adapted updater's own download
def test_the_release_is_served_as_github_serves_it(tmp_path):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        data = b'[project]\nversion = "1.99.0"\n'
        info = tarfile.TarInfo("tow-1.99.0/pyproject.toml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "tow-source.tar.gz").write_bytes(archive.getvalue())
    import hashlib

    (folder / "SHA256SUMS").write_text(
        f"{hashlib.sha256(archive.getvalue()).hexdigest()}  tow-source.tar.gz\n", encoding="utf-8"
    )
    origin, server, asked = adapter.serve(folder, "v1.99.0")
    try:
        copy = tmp_path / "update.py"
        copy.write_text(adapter.adapt_updater(SCRIPT.read_text(encoding="utf-8"), origin), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("tow_adapted_updater_served", copy)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        machine = module.System(tmp_path / "TOW" / "app")
        assert machine.latest_tag() == "v1.99.0"
        target = tmp_path / "sums"
        assert machine.download(f"{origin}/d0j/tow/releases/download/v1.99.0/SHA256SUMS", target)
        assert target.read_bytes() == (folder / "SHA256SUMS").read_bytes()
        assert not machine.download(f"{origin}/d0j/tow/releases/download/v9.9.9/SHA256SUMS", tmp_path / "none")
    finally:
        server.shutdown()
        server.server_close()
    assert "/d0j/tow/releases/latest" in asked
    assert Path(copy).is_file()
