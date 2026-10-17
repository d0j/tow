"""What a page needs from the server to follow /sync.json by itself (static/sync.js): Home's rows
carry the digest the topics area names for them (a page that opens fetches only rows that differ),
the header names a site a new topic brings by its title, the page knows its own versions, and every
signed-in page loads sync.js before the scripts that read it. Two devices: a pause on this computer
is a row the network page draws again, from the same digests."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from helpers import open_network

from tow import i18n
from tow.store import save_state
from tow.web import app, sync, templating

KEY = "k" * 40
LAN = ("192.168.1.7", 50000)
ORIGIN = {"Origin": "http://127.0.0.1"}
ROW = re.compile(r'<div class="row-wrap[^"]*" id="row-([^"]+)"[^>]*data-row-hash="([0-9a-f]{32})"')


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = sync.SAMPLE_SEC) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A fresh sampler on a clock the test moves (one sampling cycle: ``clock.tick()``)."""
    ticking = Clock()
    monkeypatch.setattr(sync, "SAMPLER", sync.Sampler(clock=ticking))
    monkeypatch.setattr(sync, "AREAS_MEMO", sync.Areas())
    with sync._BUCKETS_LOCK:
        sync._BUCKETS.clear()
    yield ticking
    with sync._BUCKETS_LOCK:
        sync._BUCKETS.clear()


def _topic(tid: str, title: str, number: int, **fields):
    return {"id": tid, "title": title, "url": f"http://rutor.info/torrent/{number}/x", "save_path": "M:/a", **fields}


def _seed() -> None:
    save_state(
        {
            "topics": [
                _topic("t1", "Show A", 101, last_ok=True, last_check="2026-10-16T10:00:00+00:00"),
                _topic("t2", "Show B", 102),
            ]
        }
    )


def _page_hashes(html: str) -> dict[str, str]:
    return dict(ROW.findall(html))


def test_home_rows_carry_the_digest_the_topics_area_names():
    """A page that opens and asks for the whole manifest fetches no row it already shows."""
    _seed()
    client = TestClient(app)
    page = _page_hashes(client.get("/").text)
    assert set(page) == {"t1", "t2"}
    manifest = client.get("/sync.json").json()["areas"]["topics"]
    assert manifest["full"] is True
    assert manifest["rows"] == page
    rows = client.get("/topics/rows", params={"ids": "t1,t2", "rev": manifest["rev"]}).json()
    assert {row["id"]: row["hash"] for row in rows["rows"]} == page
    assert all(_page_hashes(row["html"]) == {row["id"]: row["hash"]} for row in rows["rows"])


def test_a_pause_on_this_computer_is_a_row_the_network_page_draws_again(monkeypatch, clock):
    """Two devices: the network page compares its rows' digests with the manifest and fetches only
    the paused one, drawn paused, with the new digest."""
    _seed()
    open_network(monkeypatch, token=KEY)
    lan = TestClient(app, client=LAN, headers=ORIGIN)
    assert lan.post("/login", data={"password": KEY}, follow_redirects=False).status_code == 303
    local = TestClient(app, headers=ORIGIN)
    seen = _page_hashes(lan.get("/").text)
    assert local.post("/topics/t2/pause", follow_redirects=False).status_code in (302, 303)
    clock.tick()
    manifest = lan.get("/sync.json").json()["areas"]["topics"]
    differ = [tid for tid in manifest["order"] if manifest["rows"][tid] != seen.get(tid)]
    assert differ == ["t2"]
    drawn = lan.post("/topics/rows", json={"ids": differ, "rev": manifest["rev"]}).json()
    assert drawn["stale"] is False
    assert "is-paused" in drawn["rows"][0]["html"]
    assert _page_hashes(drawn["rows"][0]["html"]) == {"t2": manifest["rows"]["t2"]}
    # This computer's own page, opened now, already shows it: nothing to fetch there.
    assert _page_hashes(local.get("/").text) == manifest["rows"]


def test_the_header_area_names_each_site_by_its_title_and_the_page_has_the_words():
    """sync.js draws a site a new topic brings by its title and says every state in the page's
    words (the ones base.html writes)."""
    _seed()
    client = TestClient(app)
    header = client.get("/sync.json").json()["areas"]["header"]
    assert set(header["site_titles"]) == set(header["sites"]) == {"rutor"}
    title = re.escape(header["site_titles"]["rutor"])
    html = client.get("/").text
    assert re.search(rf'<span class="trk mut" title="{title}: [^"]+">{title}<span class="sr-only">', html)
    texts = json.loads(re.search(r'<script type="application/json" id="tow-i18n">(.*?)</script>', html).group(1))
    for key in ("base.header.site_ok", "base.header.site_warn", "base.header.not_checked"):
        assert texts[key] == i18n.t(key)
    for key in ("web.header.connected", "web.header.disconnected"):
        assert texts[key] == i18n.t(key)


def test_the_page_knows_the_versions_the_version_area_compares():
    _seed()
    client = TestClient(app)
    version = client.get("/sync.json").json()["areas"]["version"]
    html = client.get("/").text
    footer = re.search(r'<footer class="app-version" data-page-version="([^"]+)" data-static-version="([^"]+)"', html)
    assert footer is not None
    assert footer.groups() == (version["app"], version["static"])


@pytest.mark.parametrize("path", ["/", "/sites", "/settings", "/history"])
def test_every_signed_in_page_loads_sync_before_the_scripts_that_read_it(path):
    _seed()
    html = TestClient(app).get(path).text
    order = re.findall(r'<script src="/static/(\w+)\.js\?v=', html)
    assert order[0] == "sync", order
    assert order.index("sync") < order.index("messages") < order.index("app") < order.index("updates")


def test_every_template_with_app_js_but_without_sync_js_is_a_page_before_signing_in():
    """1.33.1 A23: app.js has no /health.json poll of its own any more; the header clock, the
    personal timers, Needs attention and TOW's zone take sync.js's poll. Every authenticated page
    is drawn on base.html, which loads sync.js; the only templates that load app.js without it are
    the sign-in and first-start pages, which draw none of what that poll feeds."""
    from pathlib import Path

    folder = Path(templating.__file__).resolve().parents[1] / "templates"
    texts = {path.name: path.read_text(encoding="utf-8") for path in folder.glob("*.html")}
    assert "/static/sync.js" in texts["base.html"]
    pages = {name for name, text in texts.items() if not name.startswith("_") and name != "base.html"}
    on_base = {name for name in pages if '{% extends "base.html" %}' in texts[name]}
    alone = pages - on_base
    assert alone == {"login.html", "setup.html"}, alone
    for name in alone:
        assert "/static/app.js" in texts[name]
        assert "/static/sync.js" not in texts[name]
        for fed in ('id="next-check"', "data-topic-timer", "data-attention", "data-short"):
            assert fed not in texts[name], (name, fed)
    for name, text in texts.items():
        if name.startswith("_"):  # partials: drawn inside a page, never a page of their own
            assert "/static/app.js" not in text, name


def test_the_static_version_follows_sync_js(monkeypatch, tmp_path):
    """A changed sync.js is a new static version: an open page sees it and reloads."""
    for path in templating.STATIC_DIR.iterdir():
        if path.is_file():
            (tmp_path / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    before = templating._static_asset_version()
    (tmp_path / "sync.js").write_bytes((tmp_path / "sync.js").read_bytes() + b"\n// changed\n")
    assert templating._static_asset_version() != before


def _versioned_assets() -> list[str]:
    """Every script and stylesheet a template links with the static version (``?v=``)."""
    templates = templating.PACKAGE_DIR / "templates"
    found = set()
    for page in templates.rglob("*.html"):
        found.update(re.findall(r"/static/([\w.-]+\.(?:js|css))\?v=", page.read_text(encoding="utf-8")))
    return sorted(found)


@pytest.mark.parametrize("name", _versioned_assets())
def test_every_versioned_asset_changes_the_static_version(monkeypatch, tmp_path, name):
    """Final review 1.33.0 #15: a change confined to pages.js kept its immutable address and the
    advertised static version, so open pages never loaded it."""
    for path in templating.STATIC_DIR.iterdir():
        if path.is_file():
            (tmp_path / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    before = templating._static_asset_version()
    (tmp_path / name).write_bytes((tmp_path / name).read_bytes() + b"\n/* changed */\n")
    assert templating._static_asset_version() != before
