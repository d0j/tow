"""Site settings guards: no internal tracker hosts, no catastrophic regexes."""

import re
from pathlib import Path

import pytest
import yaml
from helpers import raises_code

from tow.trackers.generic import regex_redos_risk, validate_tracker_regex
from tow.web.site_form import (
    internal_host,
    unresolved_hosts,
    unresolved_note,
    valid_site_hosts,
    valid_tracker_path,
    validate_tracker_regexes,
)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "192.168.1.2", "192.168.1.1", "169.254.1.1", "100.64.1.2", "[::1]", "nas.local"],
)
def test_internal_hosts_are_detected(host):
    assert internal_host(host)


@pytest.mark.parametrize("host", ["rutor.info", "kinozal.guru", "93.184.216.34"])
def test_public_hosts_are_not_internal(host, monkeypatch):
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])
    assert not internal_host(host)


def test_internal_tracker_hosts_are_refused_unless_enabled(monkeypatch):
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])
    monkeypatch.setattr("tow.web.services.load_config", dict)
    with pytest.raises(ValueError, match="домашнюю сеть"):
        valid_site_hosts(["http://127.0.0.1:8080"])
    assert valid_site_hosts(["https://rutor.info/"]) == ["https://rutor.info"]
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"allow_private_tracker_hosts": True})
    assert valid_site_hosts(["http://192.168.1.5:8080"]) == ["http://192.168.1.5:8080"]


def test_an_unresolved_tracker_host_is_named_as_unreachable_not_as_home_network(monkeypatch):
    # Every fetch checks the DNS answer again (tow.net_guard), so a name that does not resolve
    # now (a typo, a site the provider blocks) cannot become private later: it is saved, with
    # a note, instead of being called "this computer or the home network".
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: [])
    monkeypatch.setattr("tow.web.services.load_config", dict)

    assert valid_site_hosts(["https://unresolved.example"]) == ["https://unresolved.example"]
    assert unresolved_hosts(["https://unresolved.example", "https://93.184.216.34"]) == ["unresolved.example"]
    assert "unresolved.example" in unresolved_note(["https://unresolved.example"])
    assert unresolved_note(["https://93.184.216.34"]) == ""


def test_saving_a_site_whose_mirror_does_not_resolve_warns(monkeypatch):
    from fastapi.testclient import TestClient
    from helpers import shown

    from tow.config import load_config, save_config
    from tow.web import app

    cfg = load_config()
    cfg.setdefault("trackers", {})["mysite"] = {"url_regex": r"^https://tracker\.example/t/(\d+)$"}
    save_config(cfg)
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: [])
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post("/sites/mysite", data={"fetch_hosts": "https://blocked.example"}, follow_redirects=False)

    assert "blocked.example" in shown(response.headers["location"])
    assert load_config()["trackers"]["mysite"]["fetch_hosts"] == ["https://blocked.example"]


@pytest.mark.parametrize("pattern", ["(a+)+c", "(a*)*", "(a|aa)*c", "(?:x+){2,}", "((ab)*)+"])
def test_catastrophic_patterns_are_refused(pattern):
    assert regex_redos_risk(pattern)
    with raises_code("tracker.regex_redos", ValueError):
        validate_tracker_regex(pattern)
    with raises_code("tracker.regex_redos", ValueError):
        validate_tracker_regexes(pattern, "")


@pytest.mark.parametrize("pattern", [r"^https?://x/(\d+)(?:/.*)?$", r"[(+*)]+", r"\(a+\)+", "(ab)+", "(a?)+"])
def test_safe_patterns_pass(pattern):
    assert not regex_redos_risk(pattern)


def test_a_bounded_ambiguous_pattern_cannot_stall_topic_matching(monkeypatch):
    from tow.trackers import generic

    pattern = r"^(a|aa){30}x$"
    assert not regex_redos_risk(pattern)  # the heuristic alone cannot prove a pattern is safe
    site = generic.GenericHttpTracker("custom", {"url_regex": pattern})
    monkeypatch.setattr(generic, "MAX_URL_REGEX_SECONDS", 0.01)

    with raises_code("tracker.regex_timeout", ValueError):
        site.parse_id("a" * 60 + "y")


def test_a_bounded_ambiguous_pattern_cannot_stall_page_search(monkeypatch):
    from tow.trackers import generic

    site = generic.GenericHttpTracker(
        "custom", {"url_regex": r"^topic/(\d+)$", "download_href_regex": r"^(a|aa){30}x$"}
    )
    monkeypatch.setattr(generic, "MAX_PAGE_REGEX_SECONDS", 0.01)

    with raises_code("tracker.regex_timeout", ValueError):
        site._page_download_id("a" * 60 + "y")


def _example_site() -> dict:
    """The commented site of your own in config.example.yaml, as the loader would read it."""
    text = (Path(__file__).parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    block = text[text.index("# trackers:") :]
    data = yaml.safe_load("\n".join(line.removeprefix("# ") for line in block.splitlines()))
    return data["trackers"]["mysite"]


def test_the_example_site_of_your_own_is_a_working_site():
    from tow.trackers.generic import GenericHttpTracker

    site = GenericHttpTracker("mysite", _example_site())
    assert site.parse_id("https://tracker.example/viewtopic.php?t=42") == "42"


def test_every_template_tracker_regex_passes_the_guard():
    from tow.trackers.presets import known_sites

    for spec in [*known_sites().values(), _example_site()]:
        for key in ("url_regex", "download_href_regex"):
            if spec.get(key):
                validate_tracker_regex(spec[key])


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("the host could not be resolved safely", "doctor.reason.no_address"),
        ("the host resolves to a non-public address", "doctor.reason.home_address"),
    ],
)
def test_diagnostics_name_the_guards_findings_in_words(raw, key):
    from tow.doctor import reason_text
    from tow.i18n import t

    assert reason_text(raw) == t(key, "ru")


@pytest.mark.parametrize(("pattern", "href"), [(r"^https://tracker\.example/t/\d+$", ""), ("", r"dl\.php\?id=\d+")])
def test_a_pattern_without_a_group_for_the_number_is_refused(pattern, href):
    with pytest.raises(ValueError, match=re.escape(r"(\d+)")):
        validate_tracker_regexes(pattern, href)


@pytest.mark.parametrize("path", ["/download/", "/download/{name}", "/dl/{id}/{x}", "/dl/{"])
def test_a_download_path_without_the_number_is_refused(path):
    with pytest.raises(ValueError, match=r"\{id\}"):
        valid_tracker_path(path, label="Download path", needs_id=True)
    assert valid_tracker_path("/download/{id}", label="Download path", needs_id=True) == "/download/{id}"
    assert valid_tracker_path("/login.php", label="Login path") == "/login.php"
