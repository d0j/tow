"""Accessibility and browser-side speed of the web UI: findings of the round-5 audit (axe,
keyboard, a 2000-topic Home). Each test reads the rendered page, a CSS rule or app.js text."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tow.web import app

ORIGIN = {"Origin": "http://127.0.0.1"}
SRC = Path(__file__).parents[1] / "src" / "tow"
CSS = (SRC / "static" / "app.css").read_text(encoding="utf-8")
JS = (SRC / "static" / "app.js").read_text(encoding="utf-8")


def _rules(css: str) -> dict[str, str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return {" ".join(m.group(1).split()): m.group(2) for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", css)}


def _desktop_rules() -> dict[str, str]:
    """The rules outside any @media block."""
    flat = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL))
    return _rules(flat)


@pytest.fixture
def client():
    return TestClient(app, headers=ORIGIN)


def _seed(topics=None, **extra):
    from tow.store import save_state

    save_state(
        {
            "topics": topics
            if topics is not None
            else [
                {"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x", "save_path": "Z:\\a"},
                {"id": "t2", "title": "Other", "url": "http://rutor.info/torrent/2/y", "save_path": "Z:\\b"},
            ],
            **extra,
        }
    )


def test_many_sites_never_push_the_header_controls_off_the_screen(client):
    """A1: 53 sites made the desktop page 2096 px wide (`1fr auto 1fr`): Settings was off-screen.
    The sections and the controls keep their width; the site strip takes the rest and scrolls."""
    rules = _desktop_rules()
    assert "minmax(max-content, 1fr) minmax(0, auto) minmax(max-content, 1fr)" in rules["header.app"]
    strip = rules[".hdr-sites"]
    for declaration in ("min-width: 0", "max-width: 100%", "overflow-x: auto", "padding-right: 2.5rem"):
        assert declaration in strip
    assert "mask-image: linear-gradient(to right, black calc(100% - 2.5rem), transparent)" in strip
    assert "white-space: nowrap" in rules[".hdr-sites .trk"]
    assert "outline" in rules[".hdr-sites:focus-visible"]
    # A strip that scrolls is reached by the keyboard and named; the services are a named group.
    page = client.get("/").text
    assert re.search(r'<div class="hdr-sites" role="group" aria-label="[^"]+" tabindex="0">', page)
    assert re.search(r'<div class="hdr-svc" role="group" aria-label="[^"]+">', page)
