"""Site, mirror and topic addresses are http(s) only: a "host" such as --gpu-launcher=… must
never reach the sign-in browser as a switch, and javascript: never becomes a link."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tow.bundle import ExportImportError, _validate_config_schema, _validate_state_schema
from tow.config import ConfigError, validated, web_address
from tow.i18n import t
from tow.web.routes_topic_login import _browser_start_url

SWITCH = "--gpu-launcher=calc.exe"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("https://tracker.example", True),
        ("http://192.0.2.10:8080/forum", True),
        (SWITCH, False),
        ("javascript:alert(1)", False),
        ("file:///C:/Windows", False),
        ("https://", False),
        ("tracker.example", False),
        (None, False),
    ],
)
def test_a_web_address_is_http_or_https_with_a_host(value, ok):
    assert web_address(value) is ok


def test_a_non_http_host_and_topic_never_make_a_browser_start_address():
    # Both sides had no origin (None == None): the "host" became the browser's first argument.
    tracker = SimpleNamespace(spec={"fetch_hosts": [SWITCH], "login_path": "/x"})
    with pytest.raises(ValueError, match=re.escape(t("web.browser_auth.no_safe_login_url"))):
        _browser_start_url(tracker, SWITCH)  # type: ignore[arg-type]
    good = SimpleNamespace(spec={"fetch_hosts": ["https://tracker.example"], "login_path": "/forum/login.php"})
    assert _browser_start_url(good, "https://tracker.example/forum/viewtopic.php?t=1").startswith(  # type: ignore[arg-type]
        "https://tracker.example/forum/login.php?redirect="
    )


@pytest.mark.parametrize("key", ["fetch_hosts", "login_hosts"])
def test_config_and_import_refuse_site_hosts_that_are_not_web_addresses(key):
    raw = {"trackers": {"site": {key: ["https://tracker.example", SWITCH]}}}
    with pytest.raises(ConfigError, match=rf"trackers\.site\.{key}"):
        validated(raw)
    with pytest.raises(ExportImportError, match=rf"trackers\.site\.{key}"):
        _validate_config_schema(raw)
    assert validated({"trackers": {"site": {key: ["https://tracker.example"]}}})


@pytest.mark.parametrize("url", ["javascript:alert(1)", SWITCH, "data:text/html,x"])
def test_import_refuses_a_topic_link_that_is_not_a_web_address(url):
    state = {"topics": [{"id": "a1", "url": url}], "mirrors": {}}
    with pytest.raises(ExportImportError, match=r"topic\[0\]\.url"):
        _validate_state_schema(state)


def test_import_refuses_an_active_mirror_that_is_not_a_web_address():
    with pytest.raises(ExportImportError, match="active"):
        _validate_state_schema({"topics": [], "mirrors": {"site": {"active": "javascript:alert(1)"}}})
    _validate_state_schema({"topics": [], "mirrors": {"site": {"active": "https://tracker.example"}}})
    _validate_state_schema({"topics": [{"id": "a1", "url": "https://tracker.example/t/1"}], "mirrors": {}})
