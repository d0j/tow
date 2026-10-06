"""UI and accessibility findings of the v1.16 audit (ROADMAP 1.17, the UI review)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tow.store import load_state, save_state
from tow.web import app

ORIGIN = {"Origin": "http://127.0.0.1"}


def _topic(tid: str, url: str | None = None, **extra) -> dict:
    return {"id": tid, "title": f"Show {tid}", "url": url or f"http://rutor.info/torrent/{tid[1:]}/x", **extra}


def _seed(*topics: dict, **extra) -> None:
    save_state({"topics": list(topics), "mirrors": {}, **extra})


def _set_language(value: str) -> None:
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["language"] = value
    save_config(cfg)


@pytest.fixture
def client():
    return TestClient(app, headers=ORIGIN)


# --- H1: no words in the stylesheet ---------------------------------------------------------

CSS = (Path(__file__).parents[1] / "src" / "tow" / "static" / "app.css").read_text(encoding="utf-8")


def test_long_mirror_choice_keeps_the_full_address_and_selected_indicator(client):
    from bs4 import BeautifulSoup

    from tow.config import load_config, save_config

    host = "https://" + "a" * 63 + "." + "b" * 63 + ".example"
    cfg = load_config()
    cfg["trackers"] = {
        "fixture": {
            "url_regex": r"^https://tracker\.example/topic/(\d+)",
            "fetch_hosts": ["https://tracker.example", host],
            "download_path": "/download/{id}",
        }
    }
    save_config(cfg)
    state = load_state()
    state["mirrors"] = {"fixture": {"active": host}}
    save_state(state)

    page = BeautifulSoup(client.get("/sites").text, "html.parser")
    choices = page.select(".mirror-pick")
    assert len(choices) == 2
    chosen = choices[1]
    assert chosen.select_one('input[name="host"]')["value"] == host
    button = chosen.select_one("button")
    assert button["aria-pressed"] == "true"
    assert host in button["title"]
    label = button.select_one(".mirror-label")
    assert label.get_text() == host.removeprefix("https://")
    assert button.select_one(".pill").get_text() == "Основное"
    assert button.select_one(".pill").parent == button
    assert re.search(r"\.mirror-pick\s*\{[^}]*max-width:\s*100%", CSS)
    assert re.search(r"\.mirror-pick button\s*\{[^}]*max-width:\s*100%", CSS)
    assert re.search(r"\.mirror-pick \.mirror-label\s*\{[^}]*min-width:\s*0", CSS)
    assert re.search(r"\.mirror-pick \.mirror-label\s*\{[^}]*text-overflow:\s*ellipsis", CSS)


def test_the_stylesheet_has_no_cyrillic_and_no_words_in_content():
    assert not re.search(r"[А-Яа-яЁё]", CSS)
    values = re.findall(r"(?<![\w-])content\s*:\s*([^;}]+)", CSS)
    assert values
    for value in values:
        for quoted in re.findall(r"\"([^\"]*)\"|'([^']*)'", value):
            text = "".join(quoted)
            assert not re.search(r"[A-Za-z]{2,}", text), f"text belongs in the catalogs: content: {value}"


def test_phone_row_labels_come_from_the_page_language(client):
    _seed(_topic("t1"))
    _set_language("en")
    english = client.get("/").text
    _set_language("ru")
    russian = client.get("/").text

    for label in ('data-label="Site:"', 'data-label="Folder:"', 'data-label="Event:"', 'data-label="Progress:"'):
        assert label in english
    for label in ('data-label="Сайт:"', 'data-label="Папка:"', 'data-label="Событие:"', 'data-label="Прогресс:"'):
        assert label in russian
    assert "content: attr(data-label)" in CSS


# --- H3: history filters ------------------------------------------------------------------------


def test_history_chips_are_links_that_keep_the_search(client):
    page = client.get("/history?group=errors&q=Show+1").text

    assert 'href="/history?group=downloads&amp;q=Show+1"' in page
    assert 'href="/history?q=Show+1"' in page  # "all" drops only the group
    assert 'class="btn chip on" href="/history?group=errors&amp;q=Show+1" aria-current="true"' in page
    # Enter in the search box submits the group too (no submit button that would win instead).
    form = re.search(r'<form method="get" action="/history" role="search">(.*?)</form>', page, re.DOTALL)
    assert form is not None
    assert '<input type="hidden" name="group" value="errors">' in form.group(1)
    assert "<button" not in form.group(1)


def test_history_without_a_group_has_no_hidden_group(client):
    page = client.get("/history").text

    assert 'name="group"' not in page
    assert 'class="btn chip on" href="/history" aria-current="true"' in page


def test_get_forms_keep_their_submitter_enabled():
    js = (Path(__file__).parents[1] / "src" / "tow" / "static" / "app.js").read_text(encoding="utf-8")
    guard = js.index('(form.getAttribute("method") || "get").toLowerCase() !== "post") return;')
    assert guard < js.index("b.disabled = true;")


# --- H2 and the draft link: a refused add -------------------------------------------------------


@pytest.fixture
def no_check(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: pytest.fail("a refused add runs no check"))
    monkeypatch.setattr("tow.title.guess_topic_title", lambda _url: "")


def test_an_unknown_site_is_explained_once_with_the_next_step(client, no_check):
    response = client.post(
        "/topics/add", data={"url": "https://unknown.example/t/1", "save_path": r"M:\a"}, follow_redirects=False
    )
    location = response.headers["location"]
    page = client.get(location).text

    assert "flash=" not in location
    assert 'id="flash"' not in page  # not twice: no flash on top of the inline reason
    assert page.count("ссылка не подходит ни к одному сайту из настроек") == 1
    assert 'id="add-error" role="alert" tabindex="-1" data-focus-field="topic-url"' in page
    assert '<a href="/sites#new">Сначала добавьте этот сайт в разделе «Сайты»</a>' in page
    url_input = re.search(r'<input id="topic-url"[^>]*>', page)
    assert url_input is not None
    assert 'aria-invalid="true" aria-describedby="add-error"' in url_input.group(0)
    assert 'value="https://unknown.example/t/1"' in url_input.group(0)


def test_a_folder_problem_points_at_the_folder_field(client, no_check):
    location = client.post(
        "/topics/add", data={"url": "http://rutor.info/torrent/5/x", "save_path": "relative"}, follow_redirects=False
    ).headers["location"]
    page = client.get(location).text

    assert 'data-focus-field="topic-save-path"' in page
    assert "/sites#new" not in page
    folder = re.search(r'<input id="topic-save-path"[^>]*>', page)
    assert folder is not None
    assert 'aria-invalid="true"' in folder.group(0)


@pytest.mark.parametrize(
    ("endpoint", "data", "clean_page"),
    [
        ("/topics/add", {"url": "http://rutor.info/torrent/5/x", "save_path": "relative"}, "/"),
        ("/sites/new", {"name": "invalid name"}, "/sites"),
    ],
)
def test_cancel_refused_add_returns_clean_page_even_without_scripts(client, no_check, endpoint, data, clean_page):
    from bs4 import BeautifulSoup

    before = load_state()
    location = client.post(endpoint, data=data, follow_redirects=False).headers["location"]
    assert "?add=" in location
    page = BeautifulSoup(client.get(location).text, "html.parser")
    form = page.select_one("#new form")
    assert form is not None
    assert form.select_one("#add-error") is not None
    cancel = form.select_one(".actions a.btn.ghost")
    assert cancel is not None
    assert cancel["href"] == clean_page
    assert not cancel.has_attr("data-close-details")
    for _ in range(2):  # follow Cancel, then refresh the same clean URL
        refreshed = BeautifulSoup(client.get(cancel["href"]).text, "html.parser")
        assert refreshed.select_one("#add-error") is None
        assert not refreshed.select_one("#new").has_attr("open")
    assert load_state() == before


def test_a_crafted_link_does_not_fill_the_add_form(client):
    page = client.get(
        "/?add_error=x&draft_url=https://evil.example/t/1&draft_save_path=C:%5CEvil&add=forged-token"
    ).text

    assert "evil.example" not in page
    assert "Evil" not in page
    assert 'id="add-error"' not in page
    assert '<details class="add card" id="new">' in page


def test_the_draft_expires(client, no_check, monkeypatch):
    from tow.web import views

    location = client.post(
        "/topics/add", data={"url": "https://unknown.example/t/2", "save_path": r"M:\a"}, follow_redirects=False
    ).headers["location"]
    assert "unknown.example/t/2" in client.get(location).text
    real = views.time.monotonic
    monkeypatch.setattr(views.time, "monotonic", lambda: real() + views._ADD_DRAFTS.ttl + 1)

    assert "unknown.example/t/2" not in client.get(location).text


def test_the_draft_store_is_bounded(no_check):
    from tow.web import views

    for n in range(views._ADD_DRAFTS.limit + 10):
        views.add_refused_redirect("x", {"url": f"u{n}"})

    assert len(views._ADD_DRAFTS._records) <= views._ADD_DRAFTS.limit


def test_the_add_error_is_scrolled_clear_of_the_header_and_focused():
    js = (Path(__file__).parents[1] / "src" / "tow" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'document.getElementById("add-error")' in js
    assert "(field || addError).focus(" in js
    assert re.search(r"#new, #add-error \{ scroll-margin-top: [\d.]+rem; \}", CSS)


# --- M2: Home without 200 inline edit forms ------------------------------------------------


def test_home_rows_carry_no_edit_form_only_a_link_to_it(client):
    _seed(*[_topic(f"t{n}") for n in range(1, 51)])

    page = client.get("/").text

    assert 'id="ed-' not in page
    assert 'name="selection_value" rows="2">' not in page.split('id="topics"', 1)[1]
    assert page.count('data-edit-src="/topics/') == 50
    assert '<a href="/topics/t7/edit">Открыть редактирование</a>' in page
    # The rows stay light: ~2.5 KB each instead of ~5.8 KB with the form (measured at 200 rows).
    rows = page.split('id="topics"', 1)[1]
    assert len(rows.encode()) / 50 < 3500


def test_the_edit_panel_fragment_is_the_form_alone(client):
    _seed(_topic("t1", last_error="boom", last_check="01.10.2026 11:56"))

    response = client.get("/topics/t1/edit-panel")

    assert response.status_code == 200
    fragment = response.text
    assert fragment.lstrip().startswith('<div class="edit-panel" data-edit-loaded>')
    assert "<html" not in fragment
    assert '<form id="ed-t1" method="post" action="/topics/t1/edit"' in fragment
    assert "data-close-details" in fragment
    assert "Последняя ошибка (01.10.2026 11:56): boom" in fragment


def test_the_edit_form_also_opens_as_a_page_without_scripts(client):
    _seed(_topic("t1"))

    page = client.get("/topics/t1/edit").text

    assert "<title>TOW — Изменить раздачу</title>" in page
    assert '<form id="ed-t1" method="post" action="/topics/t1/edit"' in page
    assert '<a class="btn ghost" href="/">Отмена</a>' in page
    assert "data-close-details" not in page
    assert 'id="save-roots"' in page


def test_a_missing_topic_has_no_edit_panel(client):
    _seed()

    assert client.get("/topics/nope/edit-panel").status_code == 404
    response = client.get("/topics/nope/edit", follow_redirects=False)
    assert response.status_code == 303
    assert "flash=" in response.headers["location"]


def test_an_edit_saved_from_the_fragment_form_still_works(client, monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"qbit": "ok", "results": []})
    _seed(_topic("t1", save_path=r"M:\a"))
    fragment = client.get("/topics/t1/edit-panel").text
    assert 'name="title" value="Show t1"' in fragment

    client.post(
        "/topics/t1/edit",
        data={"title": "Renamed", "url": "http://rutor.info/torrent/1/x", "save_path": r"M:\a"},
        follow_redirects=False,
    )

    assert load_state()["topics"][0]["title"] == "Renamed"
    assert 'name="title" value="Renamed"' in client.get("/topics/t1/edit-panel").text


def test_app_js_loads_the_panel_when_a_row_opens():
    js = (Path(__file__).parents[1] / "src" / "tow" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const loadEditPanel = async (details) =>" in js
    assert 'document.addEventListener("toggle"' in js
    # "Cancel" in a panel loaded later still closes its row.
    assert 'event.target.closest?.("[data-close-details]")' in js


# --- M4: Diagnostics on phones and in words -----------------------------------------------------

_DOCTOR = {
    "python": "3.14",
    "trackers": ["rutor"],
    "qbit_host_set": False,
    "notify_set": False,
    "topics": 0,
    "qbit": "FAIL qbit secrets missing",
    "probes": [
        {"tracker": "rutor", "host": "http://rutor.info", "ok": False, "error": "[WinError 10061] connection refused"},
        {"tracker": "rutor", "host": "http://rutor.is", "ok": True, "status": 200},
        {"tracker": "rutor", "host": "http://d.rutor.info", "ok": False, "error": "http 503"},
    ],
    "autostart": {"backend": "windows", "on": True, "where": "TOW"},
}


def test_doctor_shows_results_in_words_and_keeps_the_raw_text_in_a_tooltip(client):
    _seed(doctor=_DOCTOR)

    page = client.get("/doctor").text

    assert ">не отвечает: торрент-клиент не настроен в «Настройках»</span>" in page
    assert 'title="FAIL qbit secrets missing"' in page
    assert ">соединение отклонено</span>" in page
    assert ">отвечает (200)</span>" in page
    assert ">ошибка HTTP 503</span>" in page
    assert ">FAIL" not in page


def test_doctor_in_english(client):
    _seed(doctor=_DOCTOR)
    _set_language("en")

    page = client.get("/doctor").text

    assert ">not answering: the torrent client is not set up in Settings</span>" in page
    assert ">connection refused</span>" in page
    assert ">answers (200)</span>" in page


@pytest.mark.parametrize("autostart_on", [True, False])
def test_doctor_tables_stack_into_cards_on_a_phone(client, monkeypatch, autostart_on):
    from tow.i18n import t

    _seed(doctor={**_DOCTOR, "autostart": {"on": not autostart_on}})
    monkeypatch.setattr("tow.doctor._autostart", lambda: {"on": autostart_on, "where": "test-service"})

    page = client.get("/doctor").text

    assert page.count('<table class="table table-stack">') == 1  # the sites (1.21: no task table)
    assert '<td class="mono" data-label="Зеркало">http://rutor.info</td>' in page
    key = "doctor.autostart_on" if autostart_on else "doctor.autostart_off"
    assert f">{t(key, 'ru')}</span>" in page  # current OS observation, not the cached value
    assert "<code>test-service</code>" in page
    phone = CSS.split("/* M4: on a phone", 1)[1].split("\n}\n", 1)[0]
    assert ".table-stack thead { display: none; }" in phone
    assert ".table-stack td:nth-child(2) { min-width: 0; }" in phone
    assert "content: attr(data-label)" in phone


# --- M1: sign-in and first start ----------------------------------------------------------------

TEMPLATES = Path(__file__).parents[1] / "src" / "tow" / "templates"


def test_sign_in_page_is_a_centred_card_with_the_shared_head(client):
    page = client.get("/login").text

    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in page
    assert '<meta name="theme-color"' in page
    assert '<main class="auth-shell">' in page
    assert '<section class="card auth-card"' in page
    assert "main.auth-shell {" in CSS
    assert ".auth-card { width: min(26rem, 100%); }" in CSS


@pytest.mark.parametrize("name", ["login.html", "setup.html", "base.html"])
def test_pages_share_one_head_and_use_only_existing_layout_classes(name):
    text = (TEMPLATES / name).read_text(encoding="utf-8")

    assert '{% include "_head.html" %}' in text
    assert "<meta charset" not in text  # it lives in _head.html
    for cls in re.findall(r'<(?:main|section)[^>]*class="([^"]+)"', text):
        for one in cls.split():
            assert re.search(rf"\.{re.escape(one)}\b", CSS), f"{name}: .{one} has no style in app.css"


def test_first_start_page_uses_the_card(client):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["setup_done"] = False
    save_config(cfg)

    page = client.get("/setup").text

    assert '<section class="card auth-card auth-card-wide"' in page
    assert '<link rel="icon"' in page


# --- M6, M7: no controls in <summary>; every row button says whose it is ---------------------


def test_site_mirror_choice_lives_in_the_edit_panel(client):
    page = client.get("/sites").text

    rows = [part.split("</details>", 1)[0] for part in page.split('<details class="row-edit sites')[1:]]
    row = next(row for row in rows if "ed-site-kinozal" in row)  # a site with mirrors
    summary, panel = row.split("</summary>", 1)
    assert "<button" not in summary
    assert 'class="mirror-list"' in summary
    assert 'class="mirror-choice" role="group"' in panel
    assert 'class="mirror-pick"' in panel


def test_row_buttons_have_distinct_names(client):
    _seed(_topic("t1"), _topic("t2", paused=True))

    home = client.get("/").text
    sites = client.get("/sites").text

    labels = re.findall(r'class="row-ico[^"]*"[^>]*aria-label="([^"]+)"', home)
    assert len(labels) == 6
    assert len(set(labels)) == 6
    assert "Поставить на паузу наблюдение «Show t1»" in labels
    assert "Снять с паузы наблюдение «Show t2»" in labels
    assert "Проверить раздачу «Show t2»" in labels
    site_labels = re.findall(r'class="row-ico[^"]*"[^>]*aria-label="([^"]+)"', sites)
    assert site_labels
    assert len(set(site_labels)) == len(site_labels)
    assert "Проверить зеркала сайта Rutor" in site_labels


# --- M8: header and row tap targets -----------------------------------------------------------


def _rule(selector: str) -> str:
    clean = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", clean):
        if selector in [item.strip() for item in match[1].split(",")]:
            return match[2]
    raise AssertionError(selector)


def test_header_icons_share_one_32px_rule_in_and_out_of_nav(client):
    body = _rule("header.app .ico")
    assert "width: 2rem; height: 2rem;" in body
    assert "color: var(--nav-fg);" in body
    assert "width: 2rem; height: 2rem;" in _rule(".trk-ico")
    assert "nav a.ico, nav button.ico" not in CSS  # the old nav-only rule left the gear blue
    page = client.get("/").text
    gear = re.search(r'<a href="/settings" class="([^"]+)"', page)
    assert gear is not None
    assert gear.group(1) == "ico"  # not highlighted outside Settings
    assert 'href="/settings" class="ico on"' in client.get("/settings").text


@pytest.mark.parametrize("path", ["/", "/sites", "/settings"])
@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("with_undo", [False, True])
def test_header_separates_controls_from_status_with_full_client_label(client, monkeypatch, path, language, with_undo):
    from bs4 import BeautifulSoup

    from tow.clock import iso_now

    label = "qBittorrent (" + "fixture" * 16 + ")"
    monkeypatch.setattr("tow.web.templating._default_client_label", lambda: label)
    _set_language(language)
    if with_undo:
        _seed(_topic("t1"), undo={"kind": "topic_add", "id": "t1", "ts": iso_now()})
    page = BeautifulSoup(client.get(path).text, "html.parser")
    header = page.select_one("header.app")
    controls = header.select_one(".hdr-controls")
    assert controls is not None
    assert controls.select_one('a[href="/settings"]') is not None
    assert bool(controls.select_one("#undo-form")) is with_undo
    assert not controls.select(".hdr-svc, .hdr-clock")
    client_label = header.select_one(".hdr-svc > span")
    assert client_label.get_text() == label
    assert label in client_label["title"]
    assert header.select_one(".hdr-clock").parent == controls.parent


def test_row_icons_are_32px_on_touch_screens():
    coarse = CSS.split("@media (pointer: coarse) {", 1)[1].split("}", 1)[0]
    assert ".row-ico { width: 2rem; height: 2rem;" in coarse


def test_undo_is_an_arrow_with_its_text_kept_for_screen_readers_on_a_phone():
    phone = CSS.split("@media (max-width: 480px) {", 1)[1].split("\n}\n", 1)[0]
    assert ".undo-ico { display: block; }" in phone
    assert ".undo-btn .undo-text { position: absolute; width: 1px;" in phone


# --- L1: chips, sort, file field, add-client row follow the standard ---------------------------


def test_chips_take_size_from_the_shared_button_rule():
    chip = _rule(".chip")
    assert "font-size" not in chip
    assert "height" not in chip


def test_sort_select_and_file_field_are_40px_fields():
    assert "height" not in _rule("#list-sort")
    file_rule = _rule('.portable-import-form input[type="file"]')
    assert "height: var(--field-h);" in file_rule


def test_add_client_select_sits_next_to_its_button():
    assert "flex: 0 1 20rem;" in _rule(".client-add-row select")
    assert "align-items: center;" in _rule(".client-add-row")


# --- L2: contrast ------------------------------------------------------------------------------


def _contrast(a: str, b: str) -> float:
    def lum(color: str) -> float:
        rgb = [int(color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    high, low = sorted((lum(a), lum(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def test_field_borders_and_muted_text_have_enough_contrast():
    dark = _tokens(CSS.split(":root {", 1)[1].split("}", 1)[0])
    assert _contrast(dark["field-line"], dark["panel2"]) >= 3
    assert _contrast(dark["field-line"], dark["panel"]) >= 3
    assert _contrast(dark["mut"], dark["panel"]) >= 4.5
    assert "border-color: var(--field-line)" in CSS


def test_paused_rows_use_colour_not_opacity():
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
        if "is-paused" in selector:
            assert "opacity" not in body, selector
    assert "color: var(--mut);" in _rule("details.row-edit.is-paused > summary")


# --- L3: light theme ---------------------------------------------------------------------------


def _light_block() -> str:
    return CSS.split("@media (prefers-color-scheme: light) {", 1)[1].split("\n}\n", 1)[0]


def test_every_colour_token_has_a_light_value():
    dark = CSS.split(":root {", 1)[1].split("}", 1)[0]
    colour_tokens = set(re.findall(r"--([\w-]+):\s*(?:#|rgba?\()", dark))
    light = set(re.findall(r"--([\w-]+):", _light_block()))
    assert colour_tokens - {"shadow"} <= light
    assert "html:root { color-scheme: light; }" in _light_block()
    assert "html { color-scheme: dark; }" in CSS  # dark without a preference


def test_light_theme_contrast():
    light = _tokens(_light_block())
    for text in ("fg", "mut", "acc", "ok", "warn", "bad"):
        assert _contrast(light[text], light["panel"]) >= 4.5, text
        assert _contrast(light[text], light["panel2"]) >= 4.5, text
    assert _contrast("#ffffff", light["acc2"]) >= 4.5
    assert _contrast("#ffffff", light["danger"]) >= 4.5
    assert _contrast(light["field-line"], light["panel2"]) >= 3


def test_rules_use_tokens_not_fixed_colours():
    body = re.sub(r":root \{.*?\n\}", "", CSS, count=1, flags=re.DOTALL)
    body = body.replace(_light_block(), "")
    allowed = {"#fff", "#566070", "#2a7a55", "#8a6a1d", "#8a3a3a", "#3d6db5"}  # button text, dot rings
    found = set(re.findall(r"#[0-9a-fA-F]{3,6}\b(?![\w-])", re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)))
    assert found - allowed == set()


def test_theme_colour_follows_the_scheme(client):
    page = client.get("/").text
    assert '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f4f6fa">' in page
    assert page.index('media="(prefers-color-scheme: light)"') < page.index('content="#0b0d12"')


# --- L4: the error is reachable without a mouse -------------------------------------------------


def test_the_full_error_is_in_the_opened_row_not_only_in_tooltips(client):
    error = "rutor: all hosts failed: http 503"
    _seed(_topic("t1", last_error=error, last_error_class="tracker"))

    page = client.get("/").text
    row = page.split('<div class="row-wrap', 1)[1].split('<div class="row-wrap', 1)[0]

    assert row.count(error) == 1  # once, on the visible error line (was in three tooltips)
    assert "Полный текст ошибки — в открытой строке" in row
    assert "наведени" not in page
    panel = client.get("/topics/t1/edit-panel").text
    assert f"Последняя ошибка: {error}" in panel


# --- L5: the countdown says what it counts and stays on phones ---------------------------------


def test_countdown_is_compact_with_accessible_meaning(client):
    _set_language("en")
    page = client.get("/").text
    clock = re.search(r'<span class="hdr-clock[^>]*id="next-check".*?</span></span>', page, re.DOTALL)
    assert clock is not None
    assert "data-clock-label" not in clock.group(0)
    assert 'aria-label="Global check timer"' in clock.group(0)
    assert '<span class="clock-v" data-clock-value>' in clock.group(0)
    js = (Path(__file__).parents[1] / "src" / "tow" / "static" / "app.js").read_text(encoding="utf-8")
    assert "clock.textContent =" not in js  # the label is not overwritten by the value
    assert "00:00:00" in js


def test_countdown_is_not_hidden_on_a_phone():
    phone = CSS.split("@media (max-width: 480px) {", 1)[1].split("\n}\n", 1)[0]
    assert ".hdr-clock { display: none; }" not in phone
    assert ".hdr-clock { position: absolute;" not in phone
    assert 'grid-template-areas: "nav nav right" "sites services clock"' in CSS
    assert ".hdr-clock { grid-area: clock;" in CSS
    assert ".hdr-controls { grid-area: right;" in CSS
    assert ".hdr-right { display: contents; }" in CSS
    assert "text-overflow: ellipsis;" in _rule(".hdr-svc > span")


# --- L6: backups panel ----------------------------------------------------------------------


def test_backups_are_three_cards_with_danger_restores(client, monkeypatch):
    monkeypatch.setattr(
        "tow.web.services.list_restore_points",
        lambda: [{"id": "20260924T091524Z-deadbeef", "created_at": "2026-09-24T09:15:24+00:00", "bytes": 4096}],
    )

    page = client.get("/settings").text
    transfer = page.split('id="acc-transfer"', 1)[1].split('id="acc-log"', 1)[0]

    cards = re.findall(r'<article class="integration-card backup-card[^"]*" id="(backup-\w+)"', transfer)
    assert cards == ["backup-night", "backup-manual", "backup-file"]
    manual = transfer.split('id="backup-manual"', 1)[1].split("</article>", 1)[0]
    assert 'data-confirm="' in manual
    assert '<button class="danger" type="submit">Восстановить</button>' in manual
    file_card = transfer.split('id="backup-file"', 1)[1].split("</article>", 1)[0]
    assert '<button class="danger" type="submit" name="operation" value="restore" data-confirm=' in file_card


def test_the_default_backup_folder_is_shown_once(client):
    page = client.get("/settings").text
    night = page.split('id="backup-night"', 1)[1].split("</article>", 1)[0]
    field = re.search(r'<input id="backup-path-night"[^>]*placeholder="([^"]+)"', night)
    assert field is not None
    assert night.count(field.group(1)) == 1  # the placeholder only, no "Now: <same path>"


# --- L7: network access waits for a password ----------------------------------------------------


def _network_card(page: str) -> str:
    return page.split('id="network-card"', 1)[1].split("</article>", 1)[0]


def test_network_access_cannot_be_ticked_before_a_password_is_set(client):
    card = _network_card(client.get("/settings").text)

    box = re.search(r'<input id="allow-lan"[^>]*>', card)
    assert box is not None
    assert " disabled" in box.group(0)
    assert 'aria-describedby="allow-lan-hint allow-lan-locked"' in box.group(0)
    assert "Сначала задайте пароль выше" in card
    assert '<button type="submit" disabled>' in card


def test_the_server_still_refuses_network_access_without_a_password(client):
    from tow.config import load_config

    client.post("/settings/access", data={"allow_lan": "1"}, follow_redirects=False)

    assert not load_config().get("allow_lan")


def test_network_access_can_be_ticked_once_a_password_exists(client, monkeypatch):
    import base64

    monkeypatch.setenv("TOW_MASTER_KEY", base64.urlsafe_b64encode(b"ui-audit-test-master-key-32bytes").decode())
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)
    password = "correct-horse-battery"
    client.post("/settings/password", data={"lan_password": password, "lan_password2": password})

    card = _network_card(client.get("/settings").text)

    assert "Сначала задайте пароль выше" not in card
    box = re.search(r'<input id="allow-lan"[^>]*>', card)
    assert box is not None
    assert " disabled" not in box.group(0)


# --- M5: undo of a delete keeps the order ---------------------------------------------------


def test_undo_of_a_delete_puts_the_topic_back_where_it_was(client):
    _seed(_topic("t1"), _topic("t2"), _topic("t3"))

    client.post("/topics/t2/delete", follow_redirects=False)
    assert [t["id"] for t in load_state()["topics"]] == ["t1", "t3"]
    client.post("/undo", follow_redirects=False)

    assert [t["id"] for t in load_state()["topics"]] == ["t1", "t2", "t3"]


@pytest.mark.parametrize("index", [None, -1, 99, "1", True])
def test_undo_with_a_missing_or_bad_position_appends_at_the_end(client, index):
    from tow.clock import iso_now

    undo = {"kind": "topic", "item": _topic("t2"), "ts": iso_now()}
    if index is not None:
        undo["index"] = index
    _seed(_topic("t1"), _topic("t3"), undo=undo)

    client.post("/undo", follow_redirects=False)

    assert [t["id"] for t in load_state()["topics"]] == ["t1", "t3", "t2"]
