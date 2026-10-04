from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow import releases
from tow.web import app, services


def test_version_floats_outside_header_without_duplicating_check_clock():
    page = BeautifulSoup(TestClient(app).get("/settings").text, "html.parser")
    version = page.select_one(".app-version")
    clock = page.select_one("#next-check")
    assert version is not None
    assert clock is not None
    assert version.parent is page.body
    assert version["data-page-version"] == releases.__version__
    assert clock.find_parent("header") is not None
    assert clock.parent is page.select_one(".hdr-right")
    assert len(page.select("#next-check")) == 1
    from pathlib import Path

    css = (Path(__file__).parents[1] / "src" / "tow" / "static" / "updates.css").read_text()
    assert "position: fixed;" in css
    assert "inset-inline-end:" in css
    assert "inset-block-end:" in css
    assert ".app-version.is-obstructing { visibility: hidden;" in css
    assert "flex-wrap: wrap" in css


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(releases.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(releases.time, "time", lambda: 1000 + now[0])
    monkeypatch.setattr(releases, "__version__", "1.9.9")
    return now


@pytest.mark.parametrize(
    ("latest", "available"), [("1.9.8", False), ("1.9.9", False), ("1.10.0", True), ("2.0.0", True)]
)
def test_numeric_version_comparison(monkeypatch, clock, latest, available):
    monkeypatch.setattr(releases, "_fetch_latest", lambda: latest)
    result = releases.ReleaseChecker().check()
    assert result["available"] is available
    assert result["current"] == "1.9.9"
    assert result["checked_at"] == 1100
    assert result["url"] == f"https://github.com/d0j/tow/releases/tag/v{latest}"


@pytest.mark.parametrize("version", ["1.2", "1.2.3-rc.1", "01.2.3", "1.2.3+dev", "../1.2.3", "١.٢.٣", "v", None, 123])
def test_malformed_or_preview_versions_are_not_stable(version):
    assert releases._version(version) is None


def test_automatic_cache_and_manual_rate_limit(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(releases, "_fetch_latest", lambda: calls.append(1) or "1.10.0")
    checker = releases.ReleaseChecker()
    checker.check()
    checker.check(force=True)
    clock[0] += 59
    checker.check(force=True)
    assert len(calls) == 1
    clock[0] += 1
    checker.check(force=True)
    assert len(calls) == 2
    clock[0] += releases.CHECK_INTERVAL - 1
    checker.check()
    assert len(calls) == 2
    clock[0] += 1
    checker.check()
    assert len(calls) == 3


def test_failure_is_not_up_to_date_and_backs_off(monkeypatch, clock):
    calls = []

    def failed():
        calls.append(1)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(releases, "_fetch_latest", failed)
    checker = releases.ReleaseChecker()
    for _ in range(3):
        result = checker.check()
        assert result["ok"] is False
        assert result["checked_at"] is None
    assert len(calls) == 1
    clock[0] += releases.FAILURE_INTERVAL
    checker.check()
    assert len(calls) == 2


def test_failed_refresh_preserves_known_release(monkeypatch, clock):
    monkeypatch.setattr(releases, "_fetch_latest", lambda: "1.10.0")
    checker = releases.ReleaseChecker()
    original = checker.check()
    monkeypatch.setattr(releases, "_fetch_latest", lambda: "invalid")
    clock[0] += releases.CHECK_INTERVAL
    result = checker.check()
    assert result["ok"] is False
    assert result["available"] is True
    assert result["latest"] == original["latest"]
    assert result["checked_at"] == original["checked_at"]


def test_unknown_local_build_is_not_claimed_up_to_date(monkeypatch, clock):
    monkeypatch.setattr(releases, "__version__", "0.0.0+unknown")
    monkeypatch.setattr(releases, "_fetch_latest", lambda: "1.10.0")
    assert releases.ReleaseChecker().check()["available"] is False


def test_parallel_browsers_share_one_request(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(releases, "_fetch_latest", lambda: calls.append(1) or "1.10.0")
    checker = releases.ReleaseChecker()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: checker.check(), range(16)))
    assert len(calls) == 1
    assert all(result["available"] for result in results)


def _mock_response(monkeypatch, body, status=200, headers=None):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, content=body, headers=headers)

    monkeypatch.setattr(releases, "PublicOnlyTransport", lambda: httpx.MockTransport(respond))
    return requests


def test_release_metadata_never_controls_the_destination(monkeypatch):
    body = json.dumps({"tag_name": "v1.10.0", "draft": False, "prerelease": False, "html_url": "http://localhost/"})
    requests = _mock_response(monkeypatch, body.encode())
    monkeypatch.setenv("HTTP_PROXY", "http://localhost:1")
    assert releases._fetch_latest() == "1.10.0"
    assert len(requests) == 1
    assert str(requests[0].url) == releases.LATEST_URL
    assert "authorization" not in requests[0].headers
    assert "cookie" not in requests[0].headers


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{}",
        b"not json",
        b'{"tag_name":"v1.10.0","draft":false,"prerelease":true}',
        b'{"tag_name":"v1.10.0","draft":true,"prerelease":false}',
    ],
)
def test_invalid_release_metadata_is_rejected(monkeypatch, body):
    _mock_response(monkeypatch, body)
    with pytest.raises(ValueError, match=r"not a stable|Expecting"):
        releases._fetch_latest()


@pytest.mark.parametrize("status", [302, 403, 404, 429, 500])
def test_http_errors_and_redirects_are_not_followed(monkeypatch, status):
    requests = _mock_response(monkeypatch, b"", status, {"location": "http://localhost/"})
    with pytest.raises(httpx.HTTPStatusError):
        releases._fetch_latest()
    assert len(requests) == 1


def test_oversized_metadata_is_bounded(monkeypatch):
    _mock_response(monkeypatch, b" " * (releases.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="limit"):
        releases._fetch_latest()


def test_version_is_rendered_without_any_release_request(monkeypatch):
    monkeypatch.setattr(services, "release_status", lambda **_kwargs: pytest.fail("page performed release check"))
    page = TestClient(app).get("/")
    assert page.status_code == 200
    assert f"TOW {releases.__version__}" in page.text
    assert "data-release-badge hidden" in page.text
    assert "/static/updates.js?v=" in page.text


def test_update_routes_use_service_seam_and_csrf(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "release_status", lambda **kwargs: calls.append(kwargs) or {"ok": True})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    assert client.get("/updates.json").json() == {"ok": True}
    assert client.post("/updates/check").json() == {"ok": True}
    assert calls == [{}, {"force": True}]
    assert TestClient(app).post("/updates/check").status_code == 403
    assert calls == [{}, {"force": True}]
