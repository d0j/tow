import pytest
from fastapi.testclient import TestClient
from helpers import raises_code

from tow.web import app
from tow.web.site_form import valid_site_hosts, valid_site_name


def test_invalid_tracker_regex_is_rejected_before_config_write(monkeypatch):
    cfg = {"trackers": {"demo": {"url_regex": "^ok$", "fetch_hosts": ["https://demo"]}}, "allow_lan": False}
    writes = []
    monkeypatch.setattr("tow.web.services.load_config", lambda: cfg)
    monkeypatch.setattr("tow.web.services.save_config", lambda value: writes.append(value))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    new_response = client.post(
        "/sites/new",
        data={"name": "new", "url_regex": "[", "fetch_hosts": "https://demo", "download_path": "/download/{id}"},
        follow_redirects=False,
    )
    edit_response = client.post(
        "/sites/demo",
        data={
            "fetch_hosts": "https://demo",
            "url_regex": "^ok$",
            "download_path": "/download/{id}",
            "download_href_regex": "[",
            "new_name": "demo",
        },
        follow_redirects=False,
    )

    assert new_response.status_code == 303
    assert edit_response.status_code == 303
    assert writes == []
    assert cfg["trackers"]["demo"]["url_regex"] == "^ok$"


def test_valid_tracker_regex_can_be_compiled():
    from tow.web.site_form import validate_tracker_regexes

    assert validate_tracker_regexes("^topic/(\\d+)$", "download\\?id=(\\d+)") is None
    with raises_code("tracker.regex_invalid", ValueError):
        validate_tracker_regexes("[", "")


@pytest.mark.parametrize(
    "host",
    [
        "javascript:alert(1)",
        "https://user:pass@example.org",
        "https://example.org/path",
        "https://example.org/?x=1",
        "https://example.org:bad",
    ],
)
def test_site_host_rejects_non_origin(host):
    with pytest.raises(ValueError, match="Зеркала"):
        valid_site_hosts([host])


def test_site_host_keeps_custom_port_and_rejects_duplicates():
    assert valid_site_hosts(["https://example.org:8443/"]) == ["https://example.org:8443"]
    with pytest.raises(ValueError, match="дважды"):
        valid_site_hosts(["https://example.org", "https://example.org/"])


@pytest.mark.parametrize("name", ["new", "guess", "../../x", "a-b"])
def test_reserved_or_unsafe_site_name_rejected(name):
    with pytest.raises(ValueError, match="Имя:"):
        valid_site_name(name)
