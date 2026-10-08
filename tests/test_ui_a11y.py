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
    """The rules outside any @media block (an @media block may hold an @supports block)."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
    while (start := css.find("@media")) != -1:
        depth, end = 0, css.index("{", start)
        for end in range(end, len(css)):  # noqa: B020 - `end` is where the block closes
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            if depth == 0:
                break
        css = css[:start] + css[end + 1 :]
    return _rules(css)


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


@pytest.mark.parametrize(
    ("health", "probe_ok", "site", "client_words", "clock_words"),
    [
        ({}, None, "base.header.not_checked", "base.header.not_checked", "timer.global_title"),
        (
            {"qbit_ok": True, "check_ok": True},
            True,
            "base.header.site_ok",
            "web.header.connected",
            "timer.global_title",
        ),
        (
            {"qbit_ok": False, "check_ok": False},
            False,
            "base.header.site_warn",
            "web.header.disconnected",
            "js.clock.last_attempt_failed",
        ),
    ],
)
def test_header_states_are_said_in_words_not_only_by_colour(client, health, probe_ok, site, client_words, clock_words):
    """A3: the site names, the client chip and a failed check were told apart by colour only."""
    from tow.i18n import t

    site, client_words, clock_words = t(site, "ru"), t(client_words, "ru"), t(clock_words, "ru")
    assert site in ("ещё не проверялся", "отвечает", "сбои связи")
    extra = {"health": {**health, "at_ts": 1}} if health else {}
    if probe_ok is not None:
        extra["doctor"] = {"probes": [{"tracker": "rutor", "host": "http://rutor.info", "ok": probe_ok}]}
    _seed(**extra)
    page = client.get("/").text
    sites = page[page.index('class="hdr-sites"') : page.index('class="hdr-right"')]
    assert f'title="Rutor: {site}">Rutor<span class="sr-only">: {site}</span></span>' in sites
    services = page[page.index('class="hdr-svc"') : page.index('id="next-check"')]
    assert re.search(rf'qBittorrent<span class="sr-only">: {client_words}</span></span>', services)
    clock = page[page.index('id="next-check"') : page.index("data-clock-value")]
    assert f'title="{clock_words}"><span class="sr-only" data-clock-words>{clock_words} </span>' in clock
    # app.js keeps the hidden words equal to the tooltip, and writes them only when they change.
    assert "if (clock.title !== words) clock.title = words;" in JS
    assert "clockLabel.textContent = `${words} `" in JS
    assert "clock.title = " not in JS.replace("if (clock.title !== words) clock.title = words;", "")
    # The hidden text of a scrolled-away name stays inside the strip (it made the page wider).
    assert "position: relative" in _desktop_rules()[".hdr-sites .trk"]


def test_the_progress_link_is_outside_the_rows_summary(client):
    """A2: "3/10" was a link inside <summary> (axe nested-interactive): a summary is a button and
    its content is not reachable as a link. It now lives beside the row's buttons and is laid over
    the summary's progress column; the cell keeps the same words for the layout, unseen."""
    from bs4 import BeautifulSoup

    from tow.store import save_download_history

    _seed()
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "t1": {
                    "expected": {"kind": "episodes", "total": 10, "confidence": "high"},
                    "summary": {"completed": 3, "expected": 10, "is_complete": False},
                    "items": {},
                }
            },
        }
    )
    page = BeautifulSoup(client.get("/").text, "html.parser")
    rows = page.select("#topics > .row-wrap")
    assert all(not row.select("summary a, summary button, summary [tabindex]") for row in rows)
    first = rows[0]
    link = first.select_one(":scope > .row-progress > a.download-details.progress-link")
    assert link is not None
    assert re.fullmatch(r"\d+/10", link.get_text())
    assert link["href"] == "/topics/t1/downloads.json"
    ghost = first.select_one("summary .topic-progress .progress-ghost")
    assert ghost["aria-hidden"] == "true"
    assert ghost.get_text() == link.get_text()
    assert rows[1].select_one(".row-progress") is None  # nothing downloaded: the cell says "—"
    assert rows[1].select_one(".topic-progress .mut").get_text() == "—"
    rules = _desktop_rules()
    summary_columns = re.search(r"grid-template-columns: ([^;]+);", rules[".list-head, details.row-edit > summary"])
    layer = re.search(r"\n\.row-progress \{([^}]*)\}", CSS).group(1)  # the first rule: not @supports
    assert summary_columns.group(1) in layer  # the same columns as the summary
    assert "pointer-events: none" in layer
    assert "pointer-events: auto" in rules[".row-progress > a"]
    assert "visibility: hidden" in rules[".row-wrap:has(> .row-progress) .topic-progress"]
    assert "@supports (anchor-scope: --row-summary)" in CSS
    assert "top: anchor(top); bottom: anchor(bottom)" in CSS


@pytest.mark.parametrize(
    ("lang", "labels"),
    [("en", ["Site:", "Folder:", "Event:", "Progress:"]), ("ru", ["Сайт:", "Папка:", "Событие:", "Прогресс:"])],
)
def test_each_row_cell_says_its_column_to_a_screen_reader(client, lang, labels):
    """A11: on a desktop the columns' names were only in the head row (and on a phone only in
    CSS ::before): a screen reader heard "Kinozal, D:/TV/…" without "Site" or "Folder"."""
    from bs4 import BeautifulSoup

    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)
    _seed()
    row = BeautifulSoup(client.get("/").text, "html.parser").select_one("#topics > .row-wrap")
    said = [cell.select_one(":scope > .sr-only").get_text() for cell in row.select("summary > [data-label]")]
    assert said == [f"{label} " for label in labels]
    for cell in row.select("summary > [data-label]"):
        assert cell.select_one(":scope > .sr-only").get_text().strip() == cell["data-label"]
    # The phone draws the label in CSS: an empty alternative text keeps it from being said twice.
    assert 'content: attr(data-label) / "";' in CSS
    assert "position: relative" in _desktop_rules()[".topic-event"]  # the hidden text stays in its cell


def test_links_inside_running_text_are_underlined(client):
    """A7: "More" in the Backups note was told from its sentence by colour only (axe
    link-in-text-block)."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
    rule = re.search(r":where\(([^)]*)\) a:not\(\.btn\) \{([^}]*)\}", css)
    assert rule is not None
    containers = {name.strip() for name in rule.group(1).split(",")}
    assert {".section-note", ".action-hint", ".field-hint", ".sub", ".help-page p", ".list-empty"} <= containers
    assert "text-decoration: underline" in rule.group(2)
    page = client.get("/settings?open=transfer").text
    note = re.search(r'<p class="section-note">(?:(?!</p>).)*help#export-import', page, re.DOTALL)
    assert note is not None, "the Backups note keeps its link inside the sentence"


def test_a_long_topic_name_wraps_on_a_phone():
    """A14: a phone cut long topic names with "…" though it wraps the folder and the error."""
    phone = CSS[CSS.index("  /* A phone has the width for one value per line") :]
    phone = phone[: phone.index("\n}\n")]
    rules = _rules(phone)
    name = rules[".topic-name > .clip"]
    assert "white-space: normal" in name
    assert "overflow-wrap: anywhere" in name
    assert "white-space: nowrap" in _desktop_rules()[".clip"]  # a desktop row keeps one line


def test_the_version_is_the_pages_footer(client):
    """A12: the version badge sat outside every landmark (axe region)."""
    from bs4 import BeautifulSoup

    _seed()
    for path in ("/", "/sites", "/settings"):
        page = BeautifulSoup(client.get(path).text, "html.parser")
        footer = page.select_one("body > footer.app-version")
        assert footer is not None, path
        assert footer["data-page-version"]
        assert footer.select_one("[data-release-badge]") is not None
    # The phone rule and updates.js still find it as before.
    assert "body > .app-version { position: absolute; }" in CSS
    assert 'document.querySelector(".app-version")' in (SRC / "static" / "updates.js").read_text(encoding="utf-8")


def test_scrolling_log_boxes_are_named_regions_the_keyboard_reaches(client):
    """A4: the log of the header, of Settings and the episodes box scroll but took no focus."""
    _seed()
    home = client.get("/").text
    assert '<div class="log-box" id="log-body" role="region" aria-labelledby="log-title" tabindex="0">' in home
    assert 'id="download-body" role="region" aria-labelledby="download-title" tabindex="0"' in home
    settings = client.get("/settings").text
    assert 'id="log-settings-body" role="region" aria-labelledby="settings-log-title" tabindex="0"' in settings
    assert 'id="settings-log-title"' in settings
    assert "outline" in _desktop_rules()[".log-box:focus-visible"]


def test_personal_timers_have_hidden_words_and_are_drawn_only_when_seen_and_changed(client):
    """A10/P3: each timer had an aria-label on a span without a role, and app.js rewrote the text,
    tooltip and label of all 500 timers every second (~20 ms/s on a 2000-topic Home)."""
    _seed(
        [
            {
                "id": "t1",
                "title": "Show",
                "url": "http://rutor.info/torrent/1/x",
                "save_path": "Z:\\a",
                "check_interval_min": 30,
            }
        ]
    )
    page = client.get("/").text
    timer = re.search(r'<span class="topic-timer"[^>]*>.*?</span>', page, re.DOTALL)
    assert timer is not None
    assert "aria-label" not in timer.group(0)
    assert '<span class="sr-only" data-timer-words>' in timer.group(0)
    assert 'setAttribute("aria-label", `${node.title}' not in JS
    assert "new IntersectionObserver(" in JS
    assert "for (const node of onScreen || topicTimerNodes)" in JS
    assert "if (last.text !== text) {" in JS
    assert "if (last.title !== title) {" in JS
