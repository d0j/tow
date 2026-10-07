"""1.21 UI polish: findings of the 1.20 audit (status colours, forms, first run, wording)."""

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


@pytest.fixture
def client():
    return TestClient(app, headers=ORIGIN)


def test_hidden_list_tools_are_really_hidden():
    """`.list-tools { display: flex }` beat the UA's [hidden]: the filter bar never went away."""
    rules = _rules(CSS)
    assert "display: none" in rules[".list-tools[hidden]"]
    # A single watch still hides unnecessary tools, except when a restored tracker
    # filter must remain visible so the owner can reset it.
    assert "tools.hidden = originalOrder.length < 2 && !tracker;" in JS


# --- Status-colour contract: transport trouble is amber, "not checked yet" is grey ----------------


def _seed_rutor(*, probes=None, health=None, topics=None, active=None):
    from tow.store import save_state

    state = {
        "topics": topics
        if topics is not None
        else [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x", "save_path": "Z:\\a"}],
        "mirrors": {"rutor": {"active": active}} if active else {},
    }
    if probes is not None:
        state["doctor"] = {"probes": probes}
    if health is not None:
        state["health"] = health
    save_state(state)


def test_header_site_chip_is_amber_when_its_mirrors_do_not_answer(client):
    _seed_rutor(probes=[{"tracker": "rutor", "host": "http://rutor.info", "ok": False, "error": "timed out"}])
    page = client.get("/").text
    assert '<span class="trk warn" title="Rutor">Rutor</span>' in page
    assert 'class="trk bad"' not in page.split('class="hdr-svc"')[0]


def test_header_client_chip_is_grey_until_the_client_was_asked(client):
    _seed_rutor()
    page = client.get("/").text
    services = page.split('class="hdr-svc"', 1)[1]
    assert re.search(r'<span class="trk mut" title="[^"]*">qBit', services)
    _seed_rutor(health={"qbit_ok": False, "at_ts": 1})
    services = client.get("/").text.split('class="hdr-svc"', 1)[1]
    assert re.search(r'<span class="trk bad" title="[^"]*">qBit', services)


def test_the_countdown_without_any_check_is_not_red():
    block = JS[JS.index("if (!last) {") : JS.index("const left = last + iv - Date.now();")]
    assert 'clock.classList.add("bad")' not in block
    assert 'clock.classList.toggle("bad", !checkOk)' in block


def test_sites_row_name_globe_and_mirror_dots_are_amber_for_a_refused_mirror(client):
    _seed_rutor(
        probes=[
            {"tracker": "rutor", "host": host, "ok": False, "error": "[WinError 10061] refused"}
            for host in ("http://d.rutor.info", "http://rutor.info")
        ]
    )
    page = client.get("/sites").text
    assert '<span class="trk warn">Rutor</span>' in page
    assert re.search(r'<a class="row-ico warn" href="[^"]*rutor', page)
    assert '<span class="dot warn" aria-hidden="true"></span>' in page
    assert 'class="dot bad"' not in page
    assert 'class="trk bad"' not in page


def test_site_open_link_uses_a_responding_mirror_when_the_active_one_failed(client):
    _seed_rutor(
        active="http://d.rutor.info",
        probes=[
            {"tracker": "rutor", "host": "http://d.rutor.info", "ok": False},
            {"tracker": "rutor", "host": "http://rutor.info", "ok": True},
        ],
    )
    page = client.get("/sites").text
    assert 'href="http://rutor.info"' in page
    assert 'href="http://d.rutor.info"' not in page


def test_a_site_probe_without_any_answer_is_a_warning(client, monkeypatch):
    from helpers import flash_kind

    monkeypatch.setattr(
        "tow.web.services.doctor_report",
        lambda **_kw: {"probes": [{"tracker": "rutor", "host": "http://rutor.info", "ok": False}]},
    )
    location = client.post("/sites/rutor/probe", follow_redirects=False).headers["location"]
    assert flash_kind(location) == "warn"


@pytest.mark.parametrize(
    ("cls", "kind"), [("tracker", "warn"), ("frozen", "warn"), ("qbit", "err"), ("no_path", "err")]
)
def test_added_but_the_check_failed_is_amber_only_for_transport_trouble(client, monkeypatch, cls, kind):
    from helpers import flash_kind

    def check(**kw):
        return {"results": [{"id": kw["ids"][0], "ok": False, "error": "boom", "error_class": cls}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    monkeypatch.setattr("tow.title.guess_topic_title", lambda _url: "")
    _seed_rutor(topics=[])
    response = client.post(
        "/topics/add", data={"url": "http://rutor.info/torrent/77/x", "save_path": "Z:\\a"}, follow_redirects=False
    )
    assert flash_kind(response.headers["location"]) == kind


def test_the_edit_panels_last_error_follows_the_dot(client):
    _seed_rutor(
        topics=[
            {
                "id": "t1",
                "title": "Show",
                "url": "http://rutor.info/torrent/1/x",
                "save_path": "Z:\\a",
                "last_error": "rutor: no mirror answered: timeout",
                "last_error_class": "tracker",
            },
            {
                "id": "t2",
                "title": "Other",
                "url": "http://rutor.info/torrent/2/x",
                "save_path": "Z:\\a",
                "last_error": "torrent client: refused",
                "last_error_class": "qbit",
            },
        ]
    )
    assert '<p class="form-error warn" role="note">' in client.get("/topics/t1/edit-panel").text
    assert '<p class="form-error" role="note">' in client.get("/topics/t2/edit-panel").text
    assert "color: var(--warn)" in _rules(CSS)[".form-error.warn"]


# --- A refused new site keeps what was typed ----------------------------------------------------


def test_a_refused_new_site_reopens_the_form_with_the_input_and_the_reason(client):
    import html

    response = client.post(
        "/sites/new",
        data={
            "name": "fresh",
            "url_regex": r"^https?://fresh\.example/t/(\d+)",
            "fetch_hosts": "",
            "username": "me",
            "password": "secret-pw",
            "topic_path": "/t/{id}",
        },
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert location.startswith("/sites?add=")
    assert "fresh" not in location  # a token, never the input
    page = client.get(location).text
    assert re.search(r'<details class="add card" id="new" open>', page)
    assert 'id="add-error" role="alert" tabindex="-1" data-focus-field="site-hosts"' in page
    assert "Зеркала: укажите хотя бы один адрес сайта" in html.unescape(page)
    hosts = re.search(r'<textarea id="site-hosts"[^>]*>', page)
    assert hosts is not None
    assert 'aria-invalid="true" aria-describedby="add-error"' in hosts.group(0)
    assert 'value="fresh"' in page
    assert 'value="/t/{id}"' in page
    assert 'value="me"' in page
    assert "secret-pw" not in page  # the password is never kept


def test_a_bad_advanced_field_unfolds_advanced(client, monkeypatch):
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])
    response = client.post(
        "/sites/new",
        data={"name": "fresh", "url_regex": "x", "fetch_hosts": "https://a.example", "topic_path": "relative"},
        follow_redirects=False,
    )
    page = client.get(response.headers["location"]).text
    assert '<details class="advanced" open>' in page
    assert 'data-focus-field="site-topic-path"' in page
    assert "fold.open = true" in JS


def test_site_form_messages_name_the_field_and_no_config_keys():
    import json

    for lang in ("en", "ru"):
        catalog = json.loads((SRC / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
        for key, text in catalog["web"]["site_form"].items():
            assert "config.yaml" not in text, (lang, key)
            assert "allow_private" not in text, (lang, key)


# --- Sites: the add form starts with the link; an empty list says what a site is -------------------


def test_site_form_starts_with_the_link_and_folds_the_patterns(client):
    page = client.get("/sites").text
    form = page[page.index('<form class="new-site-form"') : page.index("</form>", page.index('class="new-site-form"'))]
    assert '<h2 id="add-site-title" class="add-title">Добавить сайт</h2>' in form
    assert form.index('id="from-url"') < form.index('id="site-name"') < form.index('id="site-hosts"')
    advanced = form[form.index('<details class="advanced"') :]
    for field in ("site-regex", "site-dl", "site-login-path", "site-topic-path", "site-href-rx"):
        assert f'id="{field}"' in advanced
    assert 'class="field-narrow"' in form
    assert "details.add > summary" in _rules(CSS)
    assert "details.add summary" not in _rules(CSS)


def test_site_download_path_starts_empty_so_a_pasted_link_fills_it(client, monkeypatch):
    """1.24.0 kept the template's /download/{id} as if the owner had typed it: a pasted forum
    link left every topic of the new site downloading from a wrong address."""
    from tow.config import load_config

    monkeypatch.setattr("tow.web.services.doctor_report", lambda **_kw: {"probes": []})  # the probe after an add
    page = client.get("/sites").text
    field = re.search(r'<input id="site-dl"[^>]*>', page)
    assert field is not None
    assert 'value=""' in field.group(0)
    assert 'placeholder="/download/{id}"' in field.group(0)
    # What the browser posts after the guess fills it, and an empty field without a link.
    client.post("/sites/new", data={"from_url": "https://tracker-qa.example/viewtopic.php?t=101", "download_path": ""})
    assert load_config()["trackers"]["tracker_qa"]["download_path"] == "/download.php?id={id}"
    client.post(
        "/sites/new",
        data={"name": "plain", "url_regex": r"^https://plain\.example/t/(\d+)", "fetch_hosts": "https://plain.example"},
    )
    assert load_config()["trackers"]["plain"]["download_path"] == "/download/{id}"


def test_site_name_field_says_its_rule_before_the_browser_refuses_it(client):
    """QA 1.24.1: a Cyrillic name met only the browser's "Please match the requested format"."""
    rule = "Только строчные латинские буквы, цифры и _, без пробелов: rutor, kinozal_tv."
    page = client.get("/sites").text
    field = re.search(r'<input id="site-name"[^>]*>', page)
    assert field is not None
    assert f'title="{rule}"' in field.group(0)
    assert 'aria-describedby="site-name-hint"' in field.group(0)
    assert f'<p class="field-hint" id="site-name-hint">{rule}</p>' in page
    edit = re.search(r'<input id="site-new-name-[^"]+"[^>]*>', page)
    assert edit is not None
    assert f'title="{rule}"' in edit.group(0)
    from tow.i18n import translate

    assert translate("sites.name_hint", "en").startswith("Small Latin letters, digits and _ only")
    refused = client.post(
        "/sites/new", data={"name": "сайт", "fetch_hosts": "https://a.example"}, follow_redirects=False
    )
    page = client.get(refused.headers["location"]).text
    field = re.search(r'<input id="site-name"[^>]*>', page)
    assert field is not None
    assert 'aria-describedby="add-error site-name-hint"' in field.group(0)


def test_home_add_form_has_a_heading(client):
    assert '<h2 id="add-topic-title" class="add-title">Добавить раздачу</h2>' in client.get("/").text


def test_sites_without_any_site_explain_and_offer_the_form(client):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["trackers"] = {}
    save_config(cfg)
    page = client.get("/sites").text
    assert '<div class="empty-state">' in page
    assert "Сайтов пока нет" in page
    assert '<a class="btn" href="#new" data-open-new>' in page
    assert 'class="list-head sites"' not in page
    assert "a.plus, a[data-open-new]" in JS


# --- Adding a topic says that it is checking (the request takes up to half a minute) -------------


def test_the_add_button_shows_a_busy_state_while_the_topic_is_checked(client):
    page = client.get("/").text
    button = re.search(r'<button type="submit" data-busy-label="([^"]+)">Добавить</button>', page)
    assert button is not None
    assert button.group(1) == "Проверка…"
    assert '<p class="field-hint busy-note" data-busy-note role="status" hidden>' in page
    busy = JS[JS.index("const showBusy") : JS.index('document.addEventListener("submit"')]
    assert 'b.setAttribute("aria-busy", "true")' in busy
    assert 'form.setAttribute("aria-busy", "true")' in busy
    assert "showBusy(form, b);" in JS[JS.index('document.addEventListener("submit"') :]
    # A failed request gives the button back.
    failure = JS[JS.index("} catch (error) {") : JS.index('document.addEventListener("submit"')]
    assert 'submitter?.removeAttribute("aria-busy")' in failure
    assert "busy-spin" in _rules(CSS).get(".busy-spin", "") or ".busy-spin" in _rules(CSS)


# --- Every id is unique on every page (a label must find its own field) ---------------------------


@pytest.mark.parametrize("path", ["/", "/sites", "/settings", "/history", "/doctor", "/settings/help"])
def test_no_page_repeats_an_id(client, path):
    from tow.store import save_state

    save_state(
        {"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x", "save_path": "Z:\\a"}]}
    )
    page = client.get(path).text
    ids = re.findall(r'\sid="([^"{}]+)"', page)
    assert ids
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    assert repeated == [], (path, repeated)


# --- First start: what TOW is, the next steps, help and diagnostics in reach ------------------------


def test_first_start_page_explains_tow_and_the_next_steps(client):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg.pop("setup_done", None)
    save_config(cfg)
    page = client.get("/setup").text
    assert "TOW следит за раздачами на торрент-сайтах" in page
    assert page.count("<li>") >= 3
    assert "Подключите торрент-клиент: Настройки → Торрент-клиенты." in page
    assert page.index("Что дальше") < page.index('name="lan_password"')


def test_empty_home_shows_three_steps_with_their_state(client, monkeypatch):
    from tow.store import save_state

    save_state({"topics": []})
    page = client.get("/").text
    steps = page[page.index('class="empty-state first-steps"') :]
    assert '<a href="/settings?open=clients">Подключите торрент-клиент</a> <span class="pill warn">Не настроен' in steps
    assert '<a href="/settings?open=bots">Подключите мессенджер</a> <span class="pill mut">По желанию' in steps
    assert '<a href="#new" data-open-new>Вставьте ссылку на раздачу</a>' in steps
    assert 'class="list-head"' not in page
    monkeypatch.setattr("tow.web.routes_home._first_steps", lambda *_a: {"client": "ok", "messenger": True})
    steps = client.get("/").text
    assert '<span class="pill ok">Подключён</span>' in steps


@pytest.mark.parametrize(
    ("health", "pill", "header"),
    [
        ({}, '<span class="pill mut">Настроен, ещё не проверен', "mut"),
        ({"qbit_ok": False, "at_ts": 1}, '<span class="pill bad">Настроен, не отвечает', "bad"),
        ({"qbit_ok": True, "at_ts": 1}, '<span class="pill ok">Подключён', "ok"),
    ],
)
def test_get_started_and_the_header_agree_on_the_client(client, health, pill, header):
    from tow.store import save_secrets, save_state

    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}})
    save_state({"topics": [], "health": health})
    page = client.get("/").text

    assert pill in page[page.index('class="empty-state first-steps"') :]
    assert re.search(rf'<span class="trk {header}" title="[^"]*">qBit', page.split('class="hdr-svc"', 1)[1])
    # QA 1.24.1: Settings → Torrent clients stayed grey while the header was red.
    settings = client.get("/settings").text
    clients = settings[settings.index('id="settings-clients-title"') :]
    shown = {"mut": "qBittorrent", "bad": "qBittorrent — не отвечает", "ok": "qBittorrent"}[header]
    assert re.match(rf'[^<]*</span>\s*<span class="pill {header}">{shown}</span>', clients.split(">", 1)[1])


def test_home_with_topics_has_no_checklist(client):
    from tow.store import save_state

    save_state(
        {"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x", "save_path": "Z:\\a"}]}
    )
    page = client.get("/").text
    assert "first-steps" not in page
    assert 'class="list-head"' in page


def test_the_header_links_the_guide_and_diagnostics(client):
    page = client.get("/").text
    header = page[page.index('<header class="app">') : page.index("</header>")]
    assert re.search(r'<a href="/doctor" class="ico hdr-doctor"[^>]*title="Диагностика"', header)
    assert re.search(r'<a href="/settings/help" class="ico hdr-help"[^>]*title="Инструкция"', header)
    help_page = client.get("/settings/help").text
    head = help_page[help_page.index('<header class="app">') : help_page.index("</header>")]
    assert re.search(r'<a href="/settings/help" class="ico hdr-help on" aria-current="page"', head)
    assert not re.search(r'<a href="/settings" class="ico on"', head)


# --- Settings: titles above cards, pills that say the truth -----------------------------------------


def _summary(page: str, acc: str) -> str:
    start = page.index(f'id="acc-{acc}"')
    return page[start : page.index("</summary>", start)]


def test_settings_section_titles_are_bigger_than_card_titles_and_pills_legible():
    rules = _rules(CSS)
    section = rules[".settings-page-compact .settings-summary-title"]
    card = rules[
        ".settings-page-compact .integration-head h2, .settings-page-compact .service-restart h2,"
        " .settings-page-compact .backup-card h2"
    ]
    assert "font-size: 15px" in section
    assert "font-size: 13.5px" in card
    assert "font-size: 11px" in rules[".settings-page-compact .settings-accordion > summary .pill"]


def _interval_720_min() -> None:
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["interval_sec"] = 43200
    save_config(cfg)


def test_settings_pills_say_what_is_set_up(client):
    _interval_720_min()
    page = client.get("/settings").text
    assert '<span class="pill warn">Не настроен</span>' in _summary(page, "clients")  # no address yet
    assert '<span class="pill mut">Не подключены</span>' in _summary(page, "bots")
    assert '<span class="pill mut">12 ч</span>' in _summary(page, "intervals")  # 720 min
    # Local only: no password is no warning.
    card = page[page.index('id="password-card"') : page.index("</article>", page.index('id="password-card"'))]
    assert '<span class="pill mut">' in card
    # One backups pill (the summary), not the same pill again in the night card.
    night = page[page.index('id="backup-night"') : page.index("</article>", page.index('id="backup-night"'))]
    assert 'class="pill' not in night.split("</div>", 2)[0] + night.split("</div>", 2)[1]


def test_client_check_waits_for_a_saved_address(client):
    page = client.get("/settings").text
    button = re.search(r'<button class="ghost" type="submit" form="ping-[^"]+"[^>]*>', page)
    assert button is not None
    assert "data-needs-address disabled" in button.group(0)
    assert "checkButton.disabled = dirty || needsAddress;" in JS


def test_check_interval_and_undo_time_are_separate_fields_with_hints(client):
    _interval_720_min()
    page = client.get("/settings").text
    form = page[
        page.index('action="/settings/interval"') : page.index("</form>", page.index('action="/settings/interval"'))
    ]
    assert 'class="field-grid"' not in form
    assert 'aria-describedby="interval-hint"' in form
    assert 'aria-describedby="undo-hint"' in form
    assert "Активные раздачи без своего таймера проверяются раз в 12 ч." in form


# --- Sentence case: labels, chips, column heads and messages start with a capital -----------------


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_labels_chips_heads_and_pills_are_in_sentence_case(lang):
    import json

    catalog = json.loads((SRC / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
    groups = {
        "home.add": ("url", "name", "folder", "client", "what", "follow", "selection"),
        "home.tools": ("all", "problem", "new", "paused"),
        "home.head": ("torrent", "site", "folder", "event", "progress"),
        "history.group": ("all", "downloads", "errors", "changes", "notifications"),
        "sites": ("name", "mirrors", "url_regex", "col_site", "col_login", "login", "password"),
        "doctor": ("th_site", "th_mirror", "th_status"),
        "settings.backups": ("pill_failed", "pill_none", "default_pill"),
    }
    for section, keys in groups.items():
        node = catalog
        for part in section.split("."):
            node = node[part]
        for key in keys:
            assert node[key][:1].isupper(), (lang, section, key, node[key])


def test_a_message_is_shown_as_a_sentence(client):
    from tow.web.views import flash_location

    page = client.get(flash_location("/", "web.common.saved", "ok")).text
    assert "<span>Сохранено" in page


@pytest.mark.parametrize(
    "target", ["https://evil.example/", "//evil.example/", "/%2fevil.example/", "/\\evil.example/"]
)
def test_flash_redirect_refuses_an_external_target(target):
    from tow.web.views import flash_redirect

    location = flash_redirect(target, "", "ok").headers["location"]
    assert location == "/"


def test_flash_redirect_preserves_a_local_settings_target():
    from tow.web.views import flash_redirect

    assert flash_redirect("/settings?open=transfer", "", "ok").headers["location"] == "/settings?open=transfer"


def test_a_log_line_starts_with_a_capital():
    from tow.log import format_event

    assert format_event({"kind": "topic_add", "ts": "2026-10-01T10:00:00+00:00"})["label"][:1].isupper()


# --- Undo: in the message that offers it, with its time; Settings says a result once ---------------


def _undo_ready():
    from tow.clock import iso_now
    from tow.store import load_state, save_state

    save_state(
        {"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x", "save_path": "Z:\\a"}]}
    )
    state = load_state()
    state["undo"] = {"kind": "topic_add", "id": "t1", "ts": iso_now()}
    save_state(state)


def test_the_undo_button_sits_in_the_message_of_its_action(client):
    from tow.web.views import flash_location

    _undo_ready()
    page = client.get(flash_location("/", "web.topics.added_nothing_new", "ok")).text
    flash = page[page.index('id="flash"') : page.index("</div>", page.index('id="flash"'))]
    assert '<form method="post" action="/undo" class="flash-undo" id="undo-form" data-ttl="' in flash
    assert "data-undo-left" in flash
    assert page.count('action="/undo"') == 1  # not in the header too
    assert "можно отменить" not in page


def test_without_the_message_the_header_keeps_the_undo(client):
    _undo_ready()
    page = client.get("/").text
    header = page[page.index('<header class="app">') : page.index("</header>")]
    assert 'id="undo-form"' in header


def test_the_undo_counts_down_and_settings_shows_a_result_once():
    block = JS[JS.index('const undoForm = document.getElementById("undo-form");') :]
    assert "left.textContent = `${Math.floor(s / 60)}:" in block
    assert "panel.prepend(flash)" in JS
    assert "inlineStatus" not in JS


# --- Raw socket and HTTP-library errors are said in words; the raw text is one click away ---------

RAW_REFUSED = (
    "torrent client: ConnectionError(MaxRetryError(\"HTTPConnectionPool(host='127.0.0.1', port=8080): Max retries "
    "exceeded with url: /api/v2/app/version (Caused by NewConnectionError('<urllib3.connection.HTTPConnection "
    "object at 0x0000>: Failed to establish a new connection: [WinError 10061] No connection could be made because "
    "the target machine actively refused it'))\"))"
)


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        (RAW_REFUSED, "torrent client: соединение отклонено"),
        ("[WinError 10061] No connection could be made", "соединение отклонено"),
        ("no connection ([Errno 110] Connection timed out)", "no connection: нет ответа вовремя"),
        ("HTTPSConnectionPool(host='x', port=443): NameResolutionError(getaddrinfo failed)", "адрес не найден"),
        ("SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]'))", "ошибка сертификата"),
        ("ConnectionResetError(10054, 'An existing connection was forcibly closed')", "соединение сброшено"),
        ("ConnectionError(something unusual)", "ошибка сети"),
        ("rutor: no mirror answered: timeout", "rutor: no mirror answered: timeout"),  # already words
        # QA 1.24.1: a Russian History showed qBittorrent's English sentence and "(timed out)".
        (
            "Failed to connect to qBittorrent. Connection Error: ConnectionError(MaxRetryError('x: [WinError 10061]'))",
            "qBittorrent: соединение отклонено",
        ),
        (
            "qBittorrent: Failed to connect to qBittorrent. Connection Error: ConnectionError(MaxRetryError('x'))",
            "qBittorrent: ошибка сети",
        ),
        (
            "example: ни одно зеркало не ответило: нет связи (timed out)",
            "example: ни одно зеркало не ответило: нет связи (нет ответа вовремя)",
        ),
        ("сайт ответил ошибкой (error 503)", "сайт ответил ошибкой (error 503)"),  # not a reason: kept
    ],
)
def test_network_errors_are_said_in_words(raw, words):
    from tow import i18n
    from tow.net_errors import humanize

    i18n.use("ru")
    assert humanize(raw) == words


def test_a_topics_check_times_are_written_in_the_pages_language():
    """QA 1.24.1: the check stores its time as text in the page language of that moment; an
    English page showed the Russian "07.10.2026 12:16:13" next to its own dates."""
    from datetime import UTC, datetime

    from tow.clock import format_ui_timestamp
    from tow.config import load_config, save_config
    from tow.store import save_state

    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    save_state(
        {
            "topics": [
                {
                    "id": "t1",
                    "title": "Show",
                    "url": "http://rutor.info/torrent/1/x",
                    "save_path": "Z:\\a",
                    "last_error": "rutor: all hosts failed",
                    "last_check": "07.10.2026 12:16:13 UTC+03:00",
                    "last_ok_at": "2026-10-06 08:00:00 MSK UTC+03:00",
                }
            ]
        }
    )
    panel = TestClient(app, headers=ORIGIN).get("/topics/t1/edit-panel").text
    assert "07.10.2026" not in panel
    assert format_ui_timestamp(datetime(2026, 10, 7, 9, 16, 13, tzinfo=UTC), "en") in panel
    assert format_ui_timestamp(datetime(2026, 10, 6, 5, 0, 0, tzinfo=UTC), "en") in panel


def test_stored_times_that_are_not_dates_stay_as_they_are():
    from tow.web.views import stored_ui_time

    assert stored_ui_time("") == ""
    assert stored_ui_time("yesterday") == "yesterday"
    assert stored_ui_time("99.99.2026 12:00:00 UTC") == "99.99.2026 12:00:00 UTC"


def test_the_update_card_gets_its_times_written_by_the_server(client, monkeypatch):
    """The card wrote "checked: 07.10.2026 12:23:55" with the script's pattern and no zone, the
    rest of the page "07.10.2026 12:16:38 UTC+03:00": the server now writes the card's times too."""
    from datetime import UTC, datetime

    from tow.clock import format_ui_timestamp

    at = datetime(2026, 10, 7, 9, 23, 55, tzinfo=UTC)
    monkeypatch.setattr(
        "tow.web.services.release_status", lambda force=False: {"ok": True, "checked_at": at.timestamp()}
    )
    monkeypatch.setattr(
        "tow.web.services.web_update_status",
        lambda: {"status": "ok", "started_at": at.timestamp(), "finished_at": at.timestamp() + 11},
    )
    assert client.get("/updates.json").json()["checked_at_label"] == format_ui_timestamp(at)
    job = client.get("/updates/status").json()
    assert job["started_at_label"] == format_ui_timestamp(at)
    assert job["finished_at_label"] == format_ui_timestamp(datetime(2026, 10, 7, 9, 24, 6, tzinfo=UTC))
    updates = (SRC / "static" / "updates.js").read_text(encoding="utf-8")
    for field in ("data.checked_at_label", "job.started_at_label", "job.finished_at_label"):
        assert field in updates


def test_home_row_edit_panel_and_history_say_the_error_in_words(client):
    from tow.log import log_event
    from tow.store import save_state

    save_state(
        {
            "topics": [
                {
                    "id": "t1",
                    "title": "Show",
                    "url": "http://rutor.info/torrent/1/x",
                    "save_path": "Z:\\a",
                    "last_error": RAW_REFUSED,
                    "last_error_class": "qbit",
                }
            ]
        }
    )
    log_event("check_fail", title="Show", error=RAW_REFUSED, cls="qbit", how="auto")
    home = client.get("/").text
    assert "MaxRetryError" not in home
    assert "соединение отклонено" in home
    panel = client.get("/topics/t1/edit-panel").text
    assert "соединение отклонено" in panel
    assert '<details class="technical-details raw-error"><summary>Подробности</summary><code>' in panel
    history = client.get("/history").text
    line = history[history.index('<div class="log-line">') :]
    line = line[: line.index("</div>")]
    assert "соединение отклонено" in line
    assert '<details class="raw-error"><summary>Подробности</summary><code>' in line
    rows = client.get("/log.json").json()["rows"]
    assert rows[0]["raw"]
    assert "MaxRetryError" not in rows[0]["detail"]
    assert 'summary.textContent = t("js.log.details")' in JS


# --- Names: tab titles say the page, one name per client, a 404 page, sign-in when it is off -------


@pytest.mark.parametrize(
    ("path", "title"),
    [
        ("/", "TOW — Главная"),
        ("/sites", "TOW — Сайты"),
        ("/settings", "TOW — Настройки"),
        ("/history", "TOW — История"),
        ("/doctor", "TOW — Диагностика"),
        ("/settings/help", "TOW — Инструкция"),
        ("/login", "TOW — Вход"),
    ],
)
def test_tab_titles_name_the_page(client, path, title):
    assert f"<title>{title}</title>" in client.get(path).text


def test_the_client_has_one_name(client):
    page = client.get("/").text
    assert ">qBittorrent</span>" in page[page.index('class="hdr-svc"') :]
    assert "(qbittorrent)" not in page
    assert ">qBit<" not in page


def test_client_name_tells_two_of_a_kind_apart():
    from tow.clients.factory import client_name

    assert client_name({"id": "default", "kind": "qbittorrent"}) == "qBittorrent"
    assert client_name({"id": "nas", "kind": "qbittorrent"}) == "qBittorrent (nas)"
    assert client_name({"id": "nas", "kind": "deluge", "title": "NAS"}) == "NAS"


def test_a_missing_page_is_a_page_for_a_browser_and_json_for_a_script(client):
    page = client.get("/no-such-page", headers={"Accept": "text/html"})
    assert page.status_code == 404
    assert "<title>TOW — Страница не найдена</title>" in page.text
    assert '<a class="btn" href="/">На главную</a>' in page.text
    api = client.get("/no-such-page", headers={"Accept": "application/json"})
    assert api.status_code == 404
    assert api.json() == {"detail": "Not Found"}


def test_sign_in_page_without_network_access_links_home_instead_of_a_dead_form(client):
    page = client.get("/login").text
    assert "<form" not in page
    assert '<a class="btn" href="/">Открыть TOW</a>' in page


# --- Wording ---------------------------------------------------------------------------------------


def test_history_tells_an_empty_log_from_a_search_without_results(client):
    assert "Событий пока нет" in client.get("/history").text
    assert "Ничего не найдено." in client.get("/history?q=zzz").text


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_wording_of_the_audit(lang):
    import json

    catalog = json.loads((SRC / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
    assert "·" not in catalog["base"]["log"]["title"]
    assert "+" not in catalog["log"]["kind"]["site_add"]
    # The button is named as it is called.
    assert catalog["settings"]["backups"]["create"] in catalog["locations"]["manual"]
    # A .towx from Settings needs the same master key; tow export/import moves TOW to another key.
    file_help = catalog["help"]["s6"]["file"]
    assert "master.key" in file_help
    assert "tow export" in file_help
    assert "tow import" in file_help
    assert catalog["history"]["group"]["all"] == catalog["home"]["tools"]["all"]


# --- Visual: hints, checkbox labels, paths, phone rows, amber contrast ------------------------------


def _contrast(a: str, b: str) -> float:
    def lum(hex_: str) -> float:
        rgb = [int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    high, low = sorted((lum(a), lum(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_light_themes_amber_dot_has_3_to_1_contrast():
    light = CSS[CSS.index("@media (prefers-color-scheme: light)") :]
    amber = re.search(r"--dot-warn:\s*(#[0-9a-fA-F]{6})", light)
    assert amber is not None
    assert _contrast(amber.group(1), "#ffffff") >= 3


def test_hints_checkboxes_and_glue(client):
    rules = _rules(CSS)
    assert "margin-top: 0" in rules[".action-hint"]
    assert "font-size: 12.5px" in rules[".check-field label"]
    assert "font-size: 12.5px" in rules[".check-label"]
    page = client.get("/sites").text
    assert "<small>" not in page


def test_folder_paths_lose_their_start_not_their_last_folder(client):
    from tow.store import save_state

    save_state(
        {
            "topics": [
                {"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x", "save_path": "D:\\A\\B\\Show"}
            ]
        }
    )
    page = client.get("/").text
    assert '<span class="clip-start" title="D:\\A\\B\\Show"><bdi>D:\\A\\B\\Show</bdi></span>' in page
    assert "direction: rtl" in _rules(CSS)[".clip-start"]


def test_phone_rows_keep_the_dot_in_line_and_one_label_size():
    phone = CSS[CSS.index("/* Labels come from the language catalog (data-label)") :]
    assert "grid-template-columns: auto minmax(0, 1fr)" in phone
    assert "font: 400 12px/1.5" in phone
