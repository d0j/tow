"""Response policy also covers requests refused before route dispatch."""

import pytest
from fastapi.testclient import TestClient

from tow.bundle import MAX_BUNDLE_BYTES
from tow.torrent import MAX_TORRENT_BYTES
from tow.web import app


def assert_security_headers(response):
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("case", ["foreign-host", "closed-lan", "csrf", "session", "missing-credential", "login"])
def test_early_refusals_keep_security_headers_and_no_store(monkeypatch, case):
    config = {"allow_lan": case != "closed-lan", "bind": "0.0.0.0"}
    monkeypatch.setattr("tow.web.services.load_config", lambda: config)
    if case != "missing-credential":
        monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    client = TestClient(app, client=("192.168.1.9", 12345), base_url="http://192.168.1.2:8787")
    if case == "foreign-host":
        response = client.get("/", headers={"Host": "foreign.example"})
        expected = 403
    elif case == "csrf":
        response = client.post("/undo", headers={"Origin": "https://foreign.example"})
        expected = 403
    elif case == "login":
        response = client.get("/", headers={"Accept": "text/html", "X-Tow-Fetch": "1"}, follow_redirects=False)
        expected = 303
        assert response.headers["location"] == "/login"
    else:
        response = client.get("/health.json")
        expected = {"closed-lan": 403, "session": 401, "missing-credential": 503}[case]
    assert response.status_code == expected
    assert_security_headers(response)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("path", "limit"),
    [
        ("/content/prepare", MAX_TORRENT_BYTES + 1024 * 1024),
        ("/settings/portable/import", MAX_BUNDLE_BYTES + 2 * 1024 * 1024),
    ],
)
@pytest.mark.parametrize("length", ["missing", "invalid", "0", "-1", "oversize"])
def test_upload_refusals_keep_response_policy(path, limit, length):
    client = TestClient(app)
    request = client.build_request("POST", path, headers={"Origin": "http://127.0.0.1"})
    if length == "missing":
        del request.headers["content-length"]
    else:
        request.headers["content-length"] = str(limit + 1) if length == "oversize" else length
    response = client.send(request)
    assert response.status_code == (413 if length == "oversize" else 411)
    assert_security_headers(response)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("query", ["", "?v=test-version"])
def test_static_missing_files_are_not_cached(query):
    response = TestClient(app).get("/static/absent-test-asset.css" + query)
    assert response.status_code == 404
    assert_security_headers(response)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("query", ["", "?v=test-version"])
def test_refused_static_requests_never_get_immutable_cache(monkeypatch, query):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"allow_lan": False})
    response = TestClient(app, client=("192.168.1.9", 12345)).get("/static/app.css" + query)
    assert response.status_code == 403
    assert_security_headers(response)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("query", ["", "?v=test-version"])
def test_successful_static_files_keep_cache_policy_including_revalidation(query):
    client = TestClient(app)
    response = client.get("/static/app.css" + query)
    assert response.status_code == 200
    assert_security_headers(response)
    expected = "public, max-age=31536000, immutable" if query else "no-cache"
    assert response.headers["cache-control"] == expected
    unchanged = client.get("/static/app.css" + query, headers={"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304
    assert_security_headers(unchanged)
    assert unchanged.headers["cache-control"] == expected
