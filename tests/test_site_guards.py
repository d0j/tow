"""Site settings guards: no internal tracker hosts, no catastrophic regexes."""

from pathlib import Path

import pytest
import yaml
from helpers import raises_code

from tow.trackers.generic import regex_redos_risk, validate_tracker_regex
from tow.web.site_form import internal_host, valid_site_hosts, validate_tracker_regexes


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "192.168.1.2", "192.168.1.1", "169.254.1.1", "100.64.1.2", "[::1]", "nas.local"],
)
def test_internal_hosts_are_detected(host):
    assert internal_host(host)


@pytest.mark.parametrize("host", ["rutor.info", "kinozal.guru", "93.184.216.34"])
def test_public_hosts_are_not_internal(host):
    assert not internal_host(host)


def test_internal_tracker_hosts_are_refused_unless_enabled(monkeypatch):
    monkeypatch.setattr("tow.web.services.load_config", dict)
    with pytest.raises(ValueError, match="домашней сети"):
        valid_site_hosts(["http://127.0.0.1:8080"])
    assert valid_site_hosts(["https://rutor.info/"]) == ["https://rutor.info"]
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"allow_private_tracker_hosts": True})
    assert valid_site_hosts(["http://192.168.1.5:8080"]) == ["http://192.168.1.5:8080"]


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
