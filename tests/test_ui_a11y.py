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
    # An open row shows the words; a click on them opens the episodes, not the row's toggle.
    assert "display: none" in rules[".row-wrap:has(> details[open]) > .row-progress"]
    assert "visibility: visible" in rules[".row-wrap:has(> details[open]) .topic-progress"]
    assert 'const words = event.target.closest?.(".progress-ghost");' in JS
    # Anchor positioning cost a layout on every scroll frame of a 2000-row Home: not used.
    assert "anchor-name" not in CSS


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


def test_the_focus_comes_back_to_the_row_or_control_after_an_action(client):
    """A5: every row or header action reloads the page and the focus went back to its start.
    The rows have ids; app.js keeps the row and the action for this tab just before the next
    page opens and that page focuses the same button, the row's summary, the neighbouring row
    (a deleted row), the same header button or the message."""
    from bs4 import BeautifulSoup

    _seed()
    home = BeautifulSoup(client.get("/").text, "html.parser")
    assert [row["id"] for row in home.select("#topics > .row-wrap")] == ["row-t1", "row-t2"]
    sites = BeautifulSoup(client.get("/sites").text, "html.parser")
    assert all(row["id"].startswith("site-row-") for row in sites.select(".row-wrap"))
    submit = JS[JS.index("const submitPostForm") : JS.index("const showBusy")]
    assert submit.index("rememberFocus(form, nextUrl);") < submit.index("window.location.assign(nextUrl);")
    restore = JS[JS.index("const restoreFocus") : JS.index("const submitPostForm")]
    for step in (
        'buttonOf(row.querySelector(":scope > .row-ops"))',
        'row.querySelector(":scope > details > summary")',
        "[saved.next, saved.previous]",
        'buttonOf(document.querySelector("header.app"))',
        'document.getElementById("flash")',
        'document.getElementById("add-error")',
    ):
        assert step in restore, step
    assert "sessionStorage" in restore
    assert JS.rstrip().endswith("restoreFocus();")  # after the rows are sorted and filtered


def test_an_actions_message_is_announced_and_stays_while_in_use(client):
    """A8: a message drawn with role=status at load is not announced; app.js copies its words into
    an empty live region a moment later. A9: the message and its undo vanished under the pointer
    or the focus; they now go once both have left."""
    _seed()
    page = client.get("/").text
    assert '<div class="sr-only" id="announce" role="status"></div>' in page
    assert '<div class="sr-only" id="announce-alert" role="alert"></div>' in page
    flash = JS[JS.index('const flash = document.getElementById("flash");') : JS.index("// One /health.json poll")]
    assert '"announce-alert" : "announce"' in flash
    assert 'flash.removeAttribute("role");' in flash  # said once
    assert "live.textContent = words;" in flash
    assert "window.setTimeout(whenUnused(flash, hideFlash), ttl * 1000);" in flash
    assert 'whenUnused(undoForm.closest("#flash") || undoForm' in flash
    unused = JS[JS.index("const whenUnused") : JS.index('const flash = document.getElementById("flash");')]
    assert 'element.matches(":hover") || element.contains(document.activeElement)' in unused
    assert '"pointerleave"' in unused
    assert '"focusout"' in unused


def test_a_theme_choice_is_saved_in_the_background_without_a_reload(client):
    """A6: arrow keys move between the theme options and every move submitted the form: the page
    reloaded at each step and the focus was lost (WCAG 3.2.2). The choice is applied at once and
    saved with fetch; the pill names it and a hidden status says it was saved."""
    from tow.config import load_config

    page = client.get("/settings").text
    theme = page[page.index('id="acc-theme"') : page.index('id="acc-clients"')]
    assert '<span class="sr-only" data-theme-status role="status"></span>' in theme
    assert '<noscript><div class="settings-actions"><button type="submit">' in theme  # without scripts: Save
    block = JS[JS.index("// Settings → Theme:") : JS.index('window.addEventListener("beforeunload"')]
    assert "requestSubmit" not in block
    assert '"X-TOW-Fetch": "1"' in block
    assert "keepalive: true" in block
    assert 'status.textContent = t("js.settings.theme_saved"' in block
    assert "pill.textContent = label;" in block
    # What the background save gets: JSON, not a page to open; the choice is saved.
    saved = client.post("/settings/theme", data={"theme": "dark"}, headers={"X-TOW-Fetch": "1"})
    assert saved.status_code == 200
    assert saved.json()["redirect"].startswith("/settings?open=theme")
    assert load_config()["theme"] == "dark"


@pytest.mark.parametrize("why", ["busy", "unknown"])
def test_a_theme_save_that_failed_is_not_announced_as_saved(client, monkeypatch, why):
    """A refused background save came back as a JSON redirect like a saved one, and the status
    said "Theme saved". The redirect's message kind goes with it and the script tells them apart."""
    from tow.i18n import t
    from tow.store import StoreWriteError
    from tow.web import services

    def busy(_cfg):
        raise StoreWriteError(13, "held by another program")

    if why == "busy":
        monkeypatch.setattr(services, "save_config", busy)
    theme = "dark" if why == "busy" else "sepia"
    failed = client.post(
        "/settings/theme", data={"theme": theme}, headers={"X-TOW-Fetch": "1", "Referer": "http://testserver/settings"}
    )
    assert failed.status_code == 200
    flash = failed.json()["flash"]
    assert flash["kind"] == "err"
    assert flash["text"] == t("web.data_busy" if why == "busy" else "settings.theme.unknown")
    monkeypatch.undo()
    saved = client.post("/settings/theme", data={"theme": "light"}, headers={"X-TOW-Fetch": "1"})
    assert saved.json()["flash"]["kind"] == "ok"
    block = JS[JS.index("// Settings → Theme:") : JS.index('window.addEventListener("beforeunload"')]
    refusal = block.index('if (data.flash && data.flash.kind !== "ok") throw new Error(')
    assert refusal < block.index('t("js.settings.theme_saved"')


def test_home_search_and_sort_touch_only_what_changes():
    """P1/P2 (2000 topics): every load re-appended all rows in the order they had (~1.2 s of
    layout), and each key typed in the search re-filtered and re-highlighted every row (~250 ms
    per key). The order is applied only when it differs; typing waits for a 130 ms pause; a
    row's hidden state and a text's marks are written only when they change."""
    block = JS[JS.index("const searchable = [") : JS.index("const paintLog")]
    sort = block[block.index("const sortRows") : block.index("// P2: typing waits")]
    assert "rows.every((row, index) => row === now[index])) return;" in sort
    assert sort.index("return;") < sort.index("list.append(...rows);")
    assert "new Intl.Collator(" in block
    assert "typing = window.setTimeout(() => { announce(); remember(); }, 130);" in block
    assert "if (el.hidden === visible) el.hidden = !visible;" in block
    assert 'if ((markedWith.get(target) ?? "") === want) return;' in block
    assert "dataset.qFolded =" not in block  # the folded words live in a Map, not 2000 attributes
    assert 'querySelectorAll("[data-hl]")' not in block[block.index("const announce") :]
    # The remembered order (localStorage) still decides the first sort.
    assert 'const mode = params.get("s") ?? storedSort() ?? "";' in block
    # content-visibility on the rows cost ~2000 style recalculations at load (13 s): not used.
    assert "content-visibility" not in CSS


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
    # A timer that scrolls into view waits for the scroll to pause: a draw per frame cost a layout.
    assert "arrived = window.setTimeout(tickTimers, 150);" in JS
    assert "if (last.text !== text) {" in JS
    assert "if (last.title !== title) {" in JS


def test_the_diagnostics_table_has_no_empty_column_header(client):
    """Qa8 (axe empty-table-header): the column of the status dots had an empty <th>; a header
    cell must name its column, so that cell is a plain one."""
    _seed(
        doctor={
            "probes": [
                {"tracker": "rutor", "host": "https://rutor.info", "ok": True, "status": 200},
                {"tracker": "rutor", "host": "https://new-rutor.org", "ok": False, "error": "timed out"},
            ]
        }
    )
    page = client.get("/doctor").text
    table = page[page.index("<table") : page.index("</table>")]
    assert "<tr><td></td><th>" in table
    assert not re.search(r"<th>\s*</th>", table)


def test_the_version_has_room_at_the_end_of_every_page():
    """Qa8: the floating version steps aside for a control under it, and at the end of a long list
    the last row was always under it: the version was never seen. The page ends with room for
    it; border-box keeps a short page from growing past the window."""
    updates = (SRC / "static" / "updates.css").read_text(encoding="utf-8")
    body = _rules(updates)["body:has(> .app-version)"]
    assert "padding-bottom: 2.2rem" in body
    assert "box-sizing: border-box" in body
