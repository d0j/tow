"""The compact screens of 1.32 (Home, Sites, History, Setup, Login, Diagnostics): what each form
sends and what each page shows, read from the rendered pages and the routes they post to."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow.store import load_state
from tow.web import app

ORIGIN = {"Origin": "http://127.0.0.1"}


@pytest.fixture
def client():
    return TestClient(app, headers=ORIGIN)


def _add(client, monkeypatch, **fields):
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda _url: "")  # no site is asked
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    data = {"url": "http://rutor.info/torrent/101/show", "title": "Show", "save_path": r"M:\anime", **fields}
    return client.post("/topics/add", data=data, follow_redirects=False)


@pytest.mark.parametrize(
    ("fields", "mode"),
    [
        ({"tracking_mode": "watch", "tracking_unchecked": "once"}, "watch"),  # the box ticked
        ({"tracking_unchecked": "once"}, "once"),  # the box cleared: a checkbox sends nothing
        ({"tracking_mode": "once"}, "once"),  # the payload of 1.31 and of scripts
        ({}, "watch"),  # neither: the default stays "follow"
    ],
)
def test_follow_is_a_checkbox_that_keeps_the_watch_and_once_payload(client, monkeypatch, fields, mode):
    """Follow the topic was a yes/no select; a cleared checkbox sends no value at all, so the
    form names what a cleared box means and the route keeps reading watch/once."""
    assert _add(client, monkeypatch, **fields).status_code == 303
    assert load_state()["topics"][0]["tracking_mode"] == mode


def test_the_add_form_offers_follow_as_one_checked_box(client):
    page = BeautifulSoup(client.get("/").text, "html.parser")
    box = page.select_one("#topic-tracking-mode")
    assert (box.name, box["type"], box["name"], box["value"]) == ("input", "checkbox", "tracking_mode", "watch")
    assert box.has_attr("checked")
    assert page.select_one('#new input[type="hidden"][name="tracking_unchecked"]')["value"] == "once"
    assert page.select_one('label[for="topic-tracking-mode"]') is not None


@pytest.mark.parametrize(
    ("error", "line"),
    [
        ("qBittorrent: connection refused", "qBittorrent: connection refused"),
        ("Transmission: connection refused", "Transmission: connection refused"),
        ("connection refused", "Торрент-клиент: connection refused"),  # no name: the class says whose
    ],
)
def test_a_client_error_names_its_client_once(error, line):
    """The row said "Торрент-клиент: qBittorrent: connection refused": the class and the client
    named the same thing twice. A detail that starts with its client's name keeps only that, as
    written ("qBittorrent", never "QBittorrent")."""
    from tow.web.views import _error_line

    assert _error_line(error, "qbit") == line


def test_the_sign_in_dialog_names_the_site_and_says_where_the_password_goes(client):
    """The dialog said "Sign-in needed" over the topic's name, and "the password never goes into
    TOW's data" beside "Save and retry". It now names the site, says the password is stored
    encrypted and offers the shared eye for what is typed."""
    from tow.store import save_state

    url = "https://rutracker.org/forum/viewtopic.php?t=1234567"
    save_state({"topics": [{"id": "rt", "title": "Сериал А", "url": url, "last_error_class": "tracker_auth"}]})
    dialog = BeautifulSoup(client.get("/?credential_topic=rt").text, "html.parser").select_one("dialog")
    assert dialog.select_one("h2").get_text() == "Вход на RuTracker"
    assert dialog.select_one("p.sub").get_text() == "Сериал А"
    assert "Введите логин и пароль" not in dialog.get_text()
    password = dialog.select_one('input[name="password"]')
    assert (password["type"], password["autocomplete"]) == ("password", "current-password")
    eye = password.find_parent(class_="secret").select_one("button.reveal")
    assert (eye["type"], eye["data-reveal"], eye["aria-controls"]) == ("button", password["id"], password["id"])
    assert eye["aria-pressed"] == "false"
    note = dialog.select_one(f"#{password['aria-describedby']}").get_text()
    assert note == "Хранится зашифрованным."  # 1.32.1 T1-v: the rest goes without saying


@pytest.mark.parametrize(
    ("form", "field", "element", "advanced"),
    [
        ({"login_path": "relative"}, "login_path", "site-login-path-rutor", True),
        ({"download_href_regex": "(a*)*"}, "download_href_regex", "site-href-rx-rutor", True),
        ({"new_name": "kinozal"}, "new_name", "site-new-name-rutor", True),
        ({"fetch_hosts": "ftp://rutor.info"}, "fetch_hosts", "site-hosts-rutor", False),
    ],
)
def test_a_refused_site_save_reopens_the_site_at_its_field(client, form, field, element, advanced):
    """A refused save of a site's card went back to a closed list with a message only. It now
    reopens the site's row at the refused field: marked invalid, focused, and the Advanced part
    open when the field lives there."""
    from urllib.parse import parse_qs, urlsplit

    response = client.post("/sites/rutor", data={"fetch_hosts": "http://rutor.info", **form}, follow_redirects=False)
    location = response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert (query["open"], query["field"]) == (["rutor"], [field])
    row = BeautifulSoup(client.get(location).text, "html.parser").select_one("#site-row-rutor")
    assert row.select_one("details.row-edit").has_attr("open")
    bad = row.select_one(f"#{element}")
    assert bad["aria-invalid"] == "true"
    assert bad.has_attr("autofocus")
    assert row.select_one("details.advanced").has_attr("open") is advanced


def test_a_refused_site_save_keeps_what_was_typed_but_the_password(client):
    """The refused card showed the saved pattern, not the "(unclosed" typed: the owner could not
    see or correct the mistake. It now comes back with what was typed - never the password - for
    that card only, and not from a link someone made (the server's own record, ?edit=)."""
    from urllib.parse import parse_qs, urlsplit

    form = {
        "fetch_hosts": "http://rutor.info\nhttp://rutor.is",
        "url_regex": "(unclosed",
        "topic_path": "/typed/{id}",
        "username": "typed-user",
        "password": "typed-secret",
        "page_download": "1",
    }
    location = client.post("/sites/rutor", data=form, follow_redirects=False).headers["location"]
    assert parse_qs(urlsplit(location).query)["field"] == ["url_regex"]
    page = client.get(location).text
    assert "typed-secret" not in page
    row = BeautifulSoup(page, "html.parser").select_one("#site-row-rutor")
    assert row.select_one("#site-url-rx-rutor")["value"] == "(unclosed"
    assert row.select_one("#site-topic-path-rutor")["value"] == "/typed/{id}"
    assert row.select_one("#site-user-rutor")["value"] == "typed-user"
    assert row.select_one("#site-hosts-rutor").get_text() == "http://rutor.info\nhttp://rutor.is"
    assert row.select_one("#site-page-dl-rutor").has_attr("checked")
    assert row.select_one("#site-pass-rutor").get("value") is None
    # Another card, or the same card without the server's record, shows what is saved.
    saved = BeautifulSoup(client.get("/sites?open=rutor&field=url_regex&edit=made-up").text, "html.parser")
    assert saved.select_one("#site-url-rx-rutor")["value"] != "(unclosed"
    other = BeautifulSoup(client.get(location.replace("open=rutor", "open=kinozal")).text, "html.parser")
    assert other.select_one("#site-url-rx-rutor")["value"] != "(unclosed"


def test_the_site_card_starts_with_the_sign_in(client):
    """The user name and password were the last of nine fields; what a preset fills in now waits
    under Advanced, closed, and the mirrors stay in view."""
    row = BeautifulSoup(client.get("/sites?open=rutor").text, "html.parser").select_one("#ed-site-rutor")
    ids = [node["id"] for node in row.select("input[id], textarea[id], select[id]")]
    assert ids.index("site-user-rutor") < ids.index("site-pass-rutor") < ids.index("site-hosts-rutor")
    advanced = row.select_one("details.advanced")
    assert not advanced.has_attr("open")
    for hidden in ("site-new-name-rutor", "site-url-rx-rutor", "site-download-path-rutor", "site-login-path-rutor"):
        assert advanced.select_one(f"#{hidden}") is not None
    # No "keep as is" over a password that was never saved.
    assert not row.select_one("#site-pass-rutor").has_attr("placeholder")


@pytest.mark.parametrize("stored", [True, False])
def test_the_site_password_says_it_is_saved_only_when_it_is(client, stored):
    """The page gets whether a site's password is saved (never its value) as ``password_saved``:
    the field is then linked to the "leave empty to keep" hint; with none saved there is no hint.
    1.32.1 S1: the hint is a "saved" tag in the label, a "Leave empty to keep" placeholder and the
    form's one note under the sign-in, as on Settings."""
    from tow.store import load_secrets, save_secrets

    secrets = load_secrets()
    secrets.setdefault("trackers", {})["rutor"] = {"username": "fixture-user", **({"password": "x"} if stored else {})}
    save_secrets(secrets)
    row = BeautifulSoup(client.get("/sites?open=rutor").text, "html.parser").select_one("#ed-site-rutor")
    field = row.select_one("#site-pass-rutor")
    assert field.get("value") is None
    tag = row.select_one('label[for="site-pass-rutor"] > span.saved-tag#site-pass-saved-rutor')
    assert (tag is not None) is stored
    assert field.get("aria-describedby") == ("site-pass-saved-rutor site-keep-rutor" if stored else None)
    assert field.get("placeholder") == ("Пусто — не менять" if stored else None)
    note = row.select_one("p.keep-note#site-keep-rutor")
    assert (note is not None) is stored
    if stored:
        assert note.find_previous_sibling(class_="password-group") is not None
    assert "Сохранён · оставьте пустым" not in row.get_text()


def test_browser_sign_in_keeps_the_saved_login_whatever_the_hidden_fields_send(client):
    """With browser sign-in chosen the user name and password step aside, but a typed or
    autofilled password was still sent and replaced the saved one. The server ignores them then;
    without browser sign-in they are saved as before."""
    from tow.store import load_secrets, save_secrets

    secrets = load_secrets()
    secrets.setdefault("trackers", {})["rutor"] = {"username": "kept-user", "password": "kept-pass"}
    save_secrets(secrets)
    data = {"fetch_hosts": "http://rutor.info", "username": "typed-user", "password": "typed-pass"}
    browser = {**data, "browser_auth_present": "1", "browser_auth": "1"}
    assert client.post("/sites/rutor", data=browser, follow_redirects=False).status_code == 303
    assert load_secrets()["trackers"]["rutor"] == {"username": "kept-user", "password": "kept-pass"}
    plain = client.post("/sites/rutor", data={**data, "browser_auth_present": "1"}, follow_redirects=False)
    assert plain.status_code == 303
    assert load_secrets()["trackers"]["rutor"] == {"username": "typed-user", "password": "typed-pass"}


def test_a_new_sign_in_address_with_browser_sign_in_forgets_the_saved_password(client):
    """With browser sign-in chosen, a new sign-in address asked for the site password again - in a
    field that browser sign-in hides and disables: the card could not be saved. It is saved now
    and the saved password goes (it was never entered for that address, and browser sign-in does
    not use it); the user name stays, and the message says so. With password sign-in the field is
    in view and the refusal stays."""
    from tow.store import load_secrets, save_secrets

    secrets = load_secrets()
    secrets.setdefault("trackers", {})["rutor"] = {"username": "kept-user", "password": "kept-pass"}
    save_secrets(secrets)
    data = {"fetch_hosts": "http://rutor.info", "login_hosts": "http://rutor.info", "login_hosts_present": "1"}
    refused = client.post("/sites/rutor", data={**data, "browser_auth_present": "1"}, follow_redirects=False)
    assert "field=password" in refused.headers["location"]
    assert load_secrets()["trackers"]["rutor"]["password"] == "kept-pass"

    browser = {**data, "browser_auth_present": "1", "browser_auth": "1"}
    saved = client.post("/sites/rutor", data=browser, follow_redirects=False)
    assert "field=password" not in saved.headers["location"]
    assert load_secrets()["trackers"]["rutor"] == {"username": "kept-user"}
    page = BeautifulSoup(client.get(saved.headers["location"]).text, "html.parser")
    assert "Сохранённый пароль удалён" in page.select_one("#flash").get_text()


def test_site_credentials_are_disabled_with_browser_sign_in_and_autofilled_per_site(client):
    """The fields a chosen browser sign-in hides are disabled with it (app.js, data-enabled-by),
    and each site's login is its own autofill section: a password manager keeps one per card."""
    page = BeautifulSoup(client.get("/sites?open=rutor").text, "html.parser")
    group = page.select_one("#ed-site-rutor .password-group")
    assert (group["data-enabled-by"], group["data-enabled-when"]) == ("site-browser-rutor", "unchecked")
    assert page.select_one("#site-user-rutor")["autocomplete"] == "section-site-rutor username"
    assert page.select_one("#site-pass-rutor")["autocomplete"] == "section-site-rutor current-password"
    assert page.select_one("#site-user")["autocomplete"] == "section-new-site username"
    assert page.select_one("#site-pass")["autocomplete"] == "section-new-site current-password"


def test_a_sign_in_target_on_a_site_without_a_sign_in_page_opens_advanced(client):
    """The "site asks to sign in" link of a site without a sign-in path focuses its user name; the
    sign-in path it also needs is under Advanced, which opens for it."""
    page = client.get("/sites?open=rutor&fix=site_login:rutor").text
    row = BeautifulSoup(page, "html.parser").select_one("#site-row-rutor")
    assert row.select_one("#site-user-rutor").has_attr("autofocus")
    assert row.select_one("details.advanced").has_attr("open")


def test_the_regex_placeholders_show_single_backslashes(client):
    page = BeautifulSoup(client.get("/sites").text, "html.parser")
    for field in page.select('[name="download_href_regex"]'):
        assert field["placeholder"] == r"Напр. download\.php\?id=(\d+)"


TECHNICAL_SITE_FIELDS = (
    "from_url",
    "name",
    "new_name",
    "fetch_hosts",
    "url_regex",
    "download_path",
    "login_path",
    "topic_path",
    "download_href_regex",
    "login_hosts",
    "infohash_path",
)


def test_site_forms_say_the_code_rule_in_the_label_and_mark_examples(client):
    """1.32.1 T1-u, PH1, §4.4: the site code had a long sentence under it; its label now carries the
    rule ("Site code · a–z, 0–9, _"), the sentence stays for screen readers, the pattern's title and
    the server's refusal. Example placeholders say "e.g."; the download path's real default is
    said as text, not shown as a grey value. Technical fields switch off phone corrections."""
    page = BeautifulSoup(client.get("/sites?open=rutor").text, "html.parser")
    for code in ("site-name", "site-new-name-rutor"):
        field = page.select_one(f"#{code}")
        assert page.select_one(f'label[for="{code}"]').get_text() == "Код сайта · a–z, 0–9, _"
        hint = page.select_one(f"#{field['aria-describedby']}")
        assert "sr-only" in hint["class"]
        assert hint.get_text() == field["title"]
        assert "латинские" in field["title"]
    assert page.select_one("#site-name")["placeholder"] == "Напр. rutor"
    examples = ("site-hosts", "site-regex", "site-login-path", "site-topic-path", "site-href-rx")
    examples += ("site-login-path-rutor", "site-topic-path-rutor", "site-href-rx-rutor", "site-infohash-rutor")
    for name in examples:
        assert page.select_one(f"#{name}")["placeholder"].startswith("Напр. "), name
    download = page.select_one("#site-dl")
    assert not download.has_attr("placeholder")
    assert page.select_one(f"#{download['aria-describedby']}").get_text() == "По умолчанию: /download/{id}"
    fields = page.select(", ".join(f'[name="{name}"]' for name in TECHNICAL_SITE_FIELDS))
    assert len(fields) >= 2 * len(TECHNICAL_SITE_FIELDS) - 3  # the add form has no login hosts/new name/hash path
    for field in fields:
        assert (field.get("autocapitalize"), field.get("autocorrect"), field.get("spellcheck")) == (
            "off",
            "off",
            "false",
        ), field["id"]


def test_an_empty_history_search_offers_the_way_back_and_no_storage_note(client):
    """History said "Nothing found." in the log's monospace with no way back, and every visit
    explained the log's file sizes."""
    from tow.log import log_event

    log_event("topic_pause", topic="t1", title="Show", paused=True, how="manual")
    page = BeautifulSoup(client.get("/history?q=zzzz").text, "html.parser")
    empty = page.select_one(".history-box .history-empty")
    assert empty.get_text(" ", strip=True) == "Ничего не найдено. Показать всё"
    assert empty.select_one("a")["href"] == "/history"
    shown = BeautifulSoup(client.get("/history").text, "html.parser")
    assert shown.select(".history-box .log-line")
    assert "5 МБ" not in shown.get_text()
    assert "последние" not in shown.get_text()  # said only when the list stops at its limit
    assert "w-m" in shown.select_one("#history-q")["class"]


def test_history_says_its_limit_only_when_it_is_reached(client, monkeypatch):
    from tow.web import routes_history

    monkeypatch.setattr(routes_history, "HISTORY_LIMIT", 1)
    from tow.log import log_event

    log_event("topic_pause", topic="t1", title="Show", paused=True, how="manual")
    log_event("topic_pause", topic="t1", title="Show", paused=False, how="manual")
    page = client.get("/history").text
    assert page.count('class="log-line"') == 1
    assert "Показано только последнее 1 событие." in page


def _first_start():
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg.pop("setup_done", None)
    save_config(cfg)


def test_a_refused_first_start_keeps_what_was_typed_but_the_passwords(client):
    """A refused first start cleared every field and unticked the box; the reason stood above
    the form in lower case. The reminder and the box come back (never a password), the reason is
    a sentence and the field it is about is marked - and said right below it (AU1, 1.32.1)."""
    _first_start()
    data = {"lan_password": "river-lamp-9", "hint": "river-lamp-9!", "allow_lan": "1"}
    refused = client.post("/setup", data=data)
    assert refused.status_code == 400
    page = BeautifulSoup(refused.text, "html.parser")
    assert page.select_one("#lan-hint")["value"] == "river-lamp-9!"
    assert page.select_one("#allow-lan").has_attr("checked")
    assert not page.select_one("#lan-password").has_attr("value")
    assert page.select_one("#lan-password2") is None
    assert page.select_one("#lan-hint")["aria-invalid"] == "true"
    error = page.select_one("#setup-error")
    assert error.get_text()[:1].isupper()
    assert error.find_previous_sibling("input")["id"] == "lan-hint"  # below its field, not above the form
    assert "setup-error" in page.select_one("#lan-hint")["aria-describedby"]


def test_first_start_asks_about_other_devices_first(client):
    """The optional password was three required fields above the one real question; the question
    comes first now, and "Start" with the box clear is the old "Later - this computer only"."""
    from tow.config import load_config
    from tow.store import load_secrets

    _first_start()
    page = BeautifulSoup(client.get("/setup").text, "html.parser")
    form = page.select_one("form")
    order = [node["id"] for node in form.select("input[id]")]
    assert order.index("allow-lan") < order.index("lan-password") < order.index("lan-hint")
    assert not form.select("input[required]")
    assert page.select_one('button.reveal[data-reveal="lan-password"]') is not None
    assert page.select_one("#lan-password2") is None  # 1.32.1: one field, its eye shows what was typed
    started = client.post("/setup", data={"action": "save"}, follow_redirects=False)
    assert (started.status_code, started.headers["location"].split("?")[0]) == (303, "/")
    assert load_config()["setup_done"] is True
    assert "lan_auth" not in load_secrets()


def test_first_start_with_the_box_clear_is_this_computer_only_whatever_was_typed(client):
    """The password group is disabled with the box clear (app.js: a short hidden password blocked
    Start), and the server takes a clear box as "this computer only" even when passwords come."""
    from tow.config import load_config
    from tow.store import load_secrets

    _first_start()
    page = BeautifulSoup(client.get("/setup").text, "html.parser")
    assert page.select_one(".lan-password")["data-enabled-by"] == "allow-lan"
    data = {"action": "save", "lan_password": "short", "lan_password2": "other-pass-9", "hint": "x"}
    started = client.post("/setup", data=data, follow_redirects=False)
    assert (started.status_code, started.headers["location"].split("?")[0]) == (303, "/")
    assert load_config()["setup_done"] is True
    assert load_config().get("allow_lan") is not True
    assert "lan_auth" not in load_secrets()


def test_the_sign_in_page_has_the_eye_and_still_posts_a_form():
    """The sign-in page offers the shared eye; it loads app.js only for it, so its form keeps
    being sent as a form (data-native-submit), and the script finds its icons and texts."""
    from tow.auth import lan_password_record
    from tow.config import load_config, save_config
    from tow.store import load_secrets, save_secrets

    secrets = load_secrets()
    secrets["lan_auth"] = lan_password_record("old-horse-battery", "")
    save_secrets(secrets)
    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    lan = TestClient(app, client=("192.168.1.7", 50000), headers=ORIGIN)
    page = BeautifulSoup(lan.get("/login").text, "html.parser")
    assert page.select_one("form.auth-form").has_attr("data-native-submit")
    assert page.select_one('.secret > button.reveal[data-reveal="lan-password"]') is not None
    assert page.select_one("svg.sprite symbol#i-eye") is not None
    assert page.select_one('script[src^="/static/app.js"]') is not None
    assert "web.secret.show" in page.select_one("script#tow-i18n").get_text()
    wrong = BeautifulSoup(lan.post("/login", data={"password": "nope-nope-1"}).text, "html.parser")
    assert wrong.select_one("#lan-password")["aria-invalid"] == "true"
    # AU1: "wrong password" right below the field it is about.
    error = wrong.select_one("#login-error")
    assert "field-error" in error["class"]
    assert error.find_previous_sibling(class_="secret").select_one("#lan-password") is not None


def test_first_start_without_javascript_is_never_blocked_by_a_hidden_password(client):
    """Without app.js nothing disables the hidden password group, and its minlength stopped Start
    once a short password was typed and the box cleared; the server checks the choice itself."""
    _first_start()
    page = BeautifulSoup(client.get("/setup").text, "html.parser")
    assert page.select_one("form.setup-form").has_attr("novalidate")


def _page_texts(html: str) -> dict:
    import json

    return json.loads(BeautifulSoup(html, "html.parser").select_one("script#tow-i18n").get_text())


def test_the_sign_in_and_first_start_pages_hand_over_only_the_eyes_texts(client):
    """Before sign-in nothing tells the network TOW's time zone (_tz_name, _tz_now) or the texts of
    pages behind the sign-in: these two pages load app.js for the eye alone."""
    from tow.auth import lan_password_record
    from tow.config import load_config, save_config
    from tow.store import load_secrets, save_secrets

    _first_start()
    pages = [_page_texts(client.get("/setup").text)]
    secrets = load_secrets()
    secrets["lan_auth"] = lan_password_record("old-horse-battery", "")
    save_secrets(secrets)
    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0", setup_done=True)
    save_config(cfg)
    pages.append(_page_texts(TestClient(app, client=("192.168.1.7", 50000), headers=ORIGIN).get("/login").text))
    for texts in pages:
        assert texts
        assert all(key.startswith("web.secret.") for key in texts), sorted(texts)


def test_a_cleared_follow_box_is_saved_as_once_on_edit(client, monkeypatch):
    assert _add(client, monkeypatch).status_code == 303
    topic = load_state()["topics"][0]
    panel = BeautifulSoup(client.get(f"/topics/{topic['id']}/edit-panel").text, "html.parser")
    box = panel.select_one(f"#edit-tracking-mode-{topic['id']}")
    assert (box["type"], box["value"]) == ("checkbox", "watch")
    assert box.has_attr("checked")
    edit = {"title": "Show", "url": topic["url"], "save_path": topic["save_path"], "tracking_unchecked": "once"}
    assert client.post(f"/topics/{topic['id']}/edit", data=edit, follow_redirects=False).status_code == 303
    assert load_state()["topics"][0]["tracking_mode"] == "once"
    panel = BeautifulSoup(client.get(f"/topics/{topic['id']}/edit-panel").text, "html.parser")
    assert not panel.select_one(f"#edit-tracking-mode-{topic['id']}").has_attr("checked")


def test_the_add_form_and_the_edit_panel_keep_one_order(client):
    """1.32 QA L10: "Add new versions" and "Check every" stood between "What to download" and its
    rule in the add form, while the edit panel had the interval beside "What to download". Both
    now say what to download, its rule and files, then following and the timer."""
    from tow.store import save_state

    save_state({"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:/TV"}]})
    home = BeautifulSoup(client.get("/").text, "html.parser").select_one("#new form")
    panel = BeautifulSoup(client.get("/topics/t1/edit-panel").text, "html.parser")
    for form, prefix, suffix in ((home, "topic-", ""), (panel, "edit-", "-t1")):
        ids = [
            node.get("id") or node.get("data-content-picker", "") for node in form.select("[id], [data-content-picker]")
        ]
        ids = ["picker" if value == "" else value for value in ids]
        names = [f"{prefix}selection-mode{suffix}", f"{prefix}selection-value{suffix}", "picker"]
        names += [f"{prefix}tracking-mode{suffix}", f"{prefix}check-interval{suffix}"]
        assert [ids.index(name) for name in names] == sorted(ids.index(name) for name in names), prefix
        row = form.select_one(".follow-row")
        assert row.select_one(f"#{prefix}tracking-mode{suffix}")
        assert row.select_one(f"#{prefix}check-interval{suffix}")


def test_the_add_form_and_the_edit_panel_share_the_whole_order_and_widths(client):
    """1.32.1 H1: the edit panel put the name before the link and the folder beside the client;
    a 92-character title got 241 px. Both forms now go link, name, folder, client, what, rule,
    files, follow and timer; the name and the folder are long fields on rows of their own, and
    the technical fields keep a phone from capitalising or correcting them (§4.4)."""
    from tow.store import save_state

    save_state({"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:/TV"}]})
    home = BeautifulSoup(client.get("/").text, "html.parser").select_one("#new form")
    panel = BeautifulSoup(client.get("/topics/t1/edit-panel").text, "html.parser").select_one("form.edit-form")
    for form, prefix, suffix, path in (
        (home, "topic-", "", "topic-save-path"),
        (panel, "edit-", "-t1", "edit-path-t1"),
    ):
        ids = [node["id"] for node in form.select("[id]")]
        order = [f"{prefix}url{suffix}", f"{prefix}title{suffix}", path, f"{prefix}client{suffix}"]
        order += [f"{prefix}selection-mode{suffix}", f"{prefix}selection-value{suffix}"]
        order += [f"{prefix}tracking-mode{suffix}", f"{prefix}check-interval{suffix}"]
        assert [ids.index(name) for name in order] == sorted(ids.index(name) for name in order), prefix
        for wide in (f"{prefix}url{suffix}", f"{prefix}title{suffix}", path):
            field = form.select_one(f"#{wide}")
            assert "w-xl" in field["class"], wide
            assert field.find_parent(class_="field-row") is None, wide  # a row of its own
        rule = form.select_one(f"#{prefix}selection-value{suffix}")
        assert (rule.name, rule["rows"]) == ("textarea", "1")
        for technical in (f"{prefix}url{suffix}", path, f"{prefix}selection-value{suffix}"):
            field = form.select_one(f"#{technical}")
            assert (field["autocapitalize"], field["autocorrect"], field["spellcheck"]) == ("off", "off", "false")
        assert not form.select_one(f"#{prefix}title{suffix}").has_attr("autocapitalize")  # a name stays natural
        assert "w-xs" in form.select_one(f"#{prefix}check-interval{suffix}")["class"]


def test_the_sign_in_password_is_a_standard_password():
    """1.32 QA N1 gave the sign-in password w-m like Setup's; 1.32.1 AU1 made it fill its card (no
    class, an override in app.css). 1.32.2: every password looks the same on every screen - the
    standard 240 px with its eye, the sign-in's too (it fills its line on a phone, as all do)."""
    from tow.auth import lan_password_record
    from tow.config import load_config, save_config
    from tow.store import load_secrets, save_secrets

    secrets = load_secrets()
    secrets["lan_auth"] = lan_password_record("old-horse-battery", "")
    save_secrets(secrets)
    cfg = load_config()
    cfg["allow_lan"] = True
    save_config(cfg)
    page = BeautifulSoup(TestClient(app).get("/login").text, "html.parser")
    field = page.select_one("#lan-password")
    assert field["class"] == ["w-m"]
    assert field.find_parent(class_="secret").select_one("button.ico.reveal")["data-reveal"] == "lan-password"
    assert field.find_parent("form")["class"] == ["form-grid", "auth-form"]


def test_the_log_dialogs_title_is_one_word(client):
    """1.32 QA N5: "Журнал (обновляется каждые 2 с)" and its History link each took two lines on a
    phone; how often it refreshes is no title."""
    head = BeautifulSoup(client.get("/").text, "html.parser").select_one("#log-pop .log-pop-head")
    assert head.select_one("#log-title").get_text() == "Журнал"
    assert head.select_one("a.log-history-link")["href"] == "/history"


def test_the_interval_hint_names_the_global_interval_in_minutes(client):
    """1.32.1 H2: "Empty: global interval." did not say how often that is. The hint names it, in
    both forms; the field stays genuinely empty (no grey number that reads as a value)."""
    from tow.config import load_config, save_config
    from tow.store import save_state

    cfg = load_config()
    cfg["interval_sec"] = 1800
    save_config(cfg)
    save_state({"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:/TV"}]})
    home = BeautifulSoup(client.get("/").text, "html.parser")
    panel = BeautifulSoup(client.get("/topics/t1/edit-panel").text, "html.parser")
    for page, field in ((home, "topic-check-interval"), (panel, "edit-check-interval-t1")):
        box = page.select_one(f"#{field}")
        assert not box.has_attr("placeholder")
        assert box.get("value", "") == ""
        hint = page.select_one(f"#{box['aria-describedby'].split()[-1]}")
        assert hint.get_text() == "Пусто — общий интервал (30 мин)."


def test_the_open_row_says_its_last_event_in_full(client):
    """1.32.1 C5: a row's event time is the short form inside its <summary> (a button), so a phone
    or a keyboard could never see the full time, and the open panel did not say it either. The
    panel's top line now gives the last event with its full time (seconds and offset); the summary
    keeps no link, button, field or tab stop. H3: "Last successful check: {at}" has no stray full
    stop before the separator."""
    from tow.store import save_download_history, save_state

    at = "2026-09-12T22:58:40+03:00"
    topic = {"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:/TV"}
    save_state({"topics": [{**topic, "last_ok": True, "last_ok_at": at, "last_check": at}]})
    event = {"kind": "torrent_completed", "label": "S01E01", "at": at}
    save_download_history({"schema_version": 1, "topics": {"t1": {"last_event": event}}})
    page = BeautifulSoup(client.get("/?open=t1").text, "html.parser")
    top = page.select_one("#row-t1 .panel-top").get_text(" ", strip=True)
    assert "Последнее событие: S01E01, " in top
    assert "22:58:40" in top  # the full time, not "12 сен, 22:58"
    assert "UTC" in top
    last_ok = top.split("Последняя успешная проверка: ")[1].split(" · ")[0]
    assert last_ok
    assert not last_ok.endswith(".")
    summary = page.select_one("#row-t1 summary")
    assert not summary.select("a, button, input, select, textarea, [tabindex]")


def test_the_picker_has_one_all_files_box_instead_of_two_buttons(client):
    """1.32.1 H5: "Select all files" and "Clear selection" were two buttons over the tree. One
    root box "All files" (ticked, mixed or clear: content.js) stands at the tree's top now; the
    saved-copy note moved onto the status line, so its own line only says a copy that failed."""
    picker = BeautifulSoup(client.get("/").text, "html.parser").select_one("#new [data-content-picker]")
    box = picker.select_one("input[type=checkbox][data-content-all]")
    assert box.find_parent("label").get_text(strip=True) == "Все файлы"
    assert box.find_parent(attrs={"data-content-manual-actions": True}) is not None
    assert picker.select_one("[data-content-none]") is None
    assert not picker.select("button[data-content-all]")
    assert "сохранить не удалось" in picker.select_one("[data-content-cached]").get_text()


def test_the_pickers_torrent_file_is_chosen_in_the_pages_language(client):
    """1.32.1 B2: the picker's ".torrent file" was the browser's own field ("Choose File / No file
    chosen" in the browser's language). It is the shared file chooser now, one id per picker, and
    content.js still finds it by ``data-content-upload``."""
    from tow.store import save_state

    picker = BeautifulSoup(client.get("/").text, "html.parser").select_one("#new [data-content-picker]")
    field = picker.select_one(".file-pick > input[type=file][data-content-upload]")
    assert field is not None
    assert field["id"] == "content-upload-new"
    assert field["accept"] == ".torrent,application/x-bittorrent"
    assert (
        picker.select_one('label[for="content-upload-new"]:not(.file-button)').get_text(strip=True) == "Файл .torrent"
    )
    assert picker.select_one(".file-pick .file-button").get_text(strip=True) == "Выбрать файл"
    url = "https://rutracker.org/forum/viewtopic.php?t=1234567"
    save_state({"topics": [{"id": "t1", "title": "Show", "url": url, "save_path": "M:/TV", "hash": "a" * 40}]})
    panel = BeautifulSoup(client.get("/topics/t1/edit-panel").text, "html.parser")
    assert panel.select_one("#content-upload-t1[data-content-upload]") is not None


def test_sites_show_a_sign_in_pill_only_when_it_says_something(client):
    """1.32.1 ST1: every Sites row said "No sign-in needed" or "Sign-in not set" - a third line
    per site on a phone, and "not needed" was a guess (a sign-in path proves a form exists, not
    that the site refuses guests). Now a row says nothing by default, "Saved sign-in details"
    (neutral) when a login is stored, and "Sign-in required" (a warning) only when a check or a
    probe saw the site ask for it - also over stored details the site refused."""
    from tow.store import load_secrets, save_secrets, save_state

    secrets = load_secrets()
    secrets.setdefault("trackers", {})["rutor"] = {"username": "fixture-user", "password": "x"}
    secrets["trackers"]["kinozal"] = {"username": "fixture-user", "password": "x"}
    save_secrets(secrets)
    url = "https://rutracker.org/forum/viewtopic.php?t=1234567"
    kinozal = "https://kinozal.tv/details.php?id=1234567"
    save_state(
        {
            "topics": [
                {"id": "rt", "title": "Сериал А", "url": url, "last_error_class": "tracker_auth"},
                {"id": "kz", "title": "Сериал Б", "url": kinozal, "last_error_class": "tracker_auth"},
            ]
        }
    )
    page = BeautifulSoup(client.get("/sites").text, "html.parser")

    def pills(name: str) -> list[tuple[str, str]]:
        summary = page.select_one(f"#site-row-{name} summary")
        return [(" ".join(p["class"]), p.get_text()) for p in summary.find_all("span", class_="pill", recursive=False)]

    assert pills("rutor") == [("pill mut", "Реквизиты сохранены")]
    assert pills("rutracker") == [("pill warn", "Нужен вход")]
    assert pills("kinozal") == [("pill warn", "Нужен вход")]
    assert pills("nnmclub") == []
    assert "Вход не нужен" not in page.get_text()
    assert "Вход не задан" not in page.get_text()


def _solid(scope) -> list[str]:
    """The solid (primary) buttons and button links of a part of a page (square icons aside)."""
    controls = scope.select("button:not(.ico), a.btn")
    return [c.get_text(" ", strip=True) for c in controls if "ghost" not in (c.get("class") or [])]


def test_edit_panels_have_one_primary_and_a_quiet_confirmed_delete(client):
    """1.32.1 V2/UI:C1: a topic's and a site's panel had a solid Save, solid fix buttons and a solid
    red Delete. Save is the one solid button now; Delete is ghost-danger, and its form names the
    confirm button's words and marks it destructive for the shared confirmation dialog."""
    from tow.store import save_state

    url = "https://rutracker.org/forum/viewtopic.php?t=1234567"
    topic = {"id": "t1", "title": "Show", "url": url, "save_path": "M:/TV", "hash": "a" * 40}
    save_state({"topics": [{**topic, "last_ok": False, "last_error": "sign-in", "last_error_class": "tracker_auth"}]})
    panel = BeautifulSoup(client.get("/topics/t1/edit-panel").text, "html.parser")
    assert _solid(panel) == ["Сохранить"]
    delete = panel.select_one('button[form="del-t1"]')
    assert delete["class"] == ["ghost", "danger"]
    form = panel.select_one("form#del-t1")
    assert (form["data-confirm-label"], form.has_attr("data-confirm-danger")) == ("Убрать из TOW", True)
    assert form["data-confirm"]
    row = BeautifulSoup(client.get("/sites?open=rutor").text, "html.parser").select_one("#site-row-rutor .edit-panel")
    assert _solid(row) == ["Сохранить"]
    delete = row.select_one('button[form="del-site-rutor"]')
    assert delete["class"] == ["ghost", "danger"]
    form = row.select_one("form#del-site-rutor")
    assert (form["data-confirm-label"], form.has_attr("data-confirm-danger")) == ("Удалить", True)


def test_check_results_put_their_time_beside_the_title_and_say_less(client):
    """1.32.1 CR1, T1-y: the run's time stood on a line of its own under "Results", and two long
    sentences explained that the report is not the current state. The time is in the title line
    now ("Results · 20:31", its full time in the tooltip), and one short line says the rest."""
    import time

    from tow.web import check_jobs
    from tow.web.check_actions import _job_result

    rows = [{"id": "t1", "title": "Сериал А", "ok": False, "error": "qBittorrent: connection refused"}]
    job = {"id": "job-cr1", "topic_id": "", "apply": True, "adopted": False, "lang": "ru", "status": "running"}
    job.update(requested_at=time.time(), subject="")
    flash, status, kind, feedback = _job_result(job, {"results": rows})
    job.update(status=status, flash=flash, kind=kind, feedback=feedback, finished_at=time.monotonic())
    job["ended_at"] = time.time()
    with check_jobs.LOCK:
        check_jobs.JOBS[job["id"]] = job
    try:
        card = BeautifulSoup(client.get("/?check_result=job-cr1").text, "html.parser").select_one("#check-result")
    finally:
        with check_jobs.LOCK:
            check_jobs.JOBS.pop(job["id"], None)
    title = card.select_one(".check-results-head h2")
    assert title.select_one("time[datetime]") is not None
    assert title.get_text(" ", strip=True).startswith("Результаты ·")
    assert not card.select("p.sub > time")  # no line of its own
    assert "Итоги этой проверки. Текущее состояние — в строке раздачи." in card.get_text()
    assert "Нажмите на раздачу" not in card.get_text()


def test_diagnostics_cards_all_have_a_heading_and_a_bounded_text_column(client):
    """1.32.1 DG1: the torrent client's card had a plain-text title while the others had headings,
    and its notes ran 1360 px wide. Every card has an h2 now, and the page's content sits in one
    wrapper whose paragraphs the stylesheet caps (``.doctor-page p``)."""
    from tow.store import save_state

    save_state({"doctor": {"probes": [], "qbit": "FAIL connection refused", "ok": False}})
    page = BeautifulSoup(client.get("/doctor").text, "html.parser")
    wrapper = page.select_one("main .doctor-page")
    assert wrapper is not None
    titles = [h.get_text(strip=True) for h in wrapper.select(".card > h2.section-title")]
    assert titles[0] == "Торрент-клиент"
    assert len(titles) == len(wrapper.select(".card:not(.stat)"))
    assert not wrapper.select(".card:not(.stat) > .k")  # stat tiles keep their small label


def test_history_lines_mark_their_time_and_keep_code_for_raw_errors(client):
    """1.32.1 H6: History was monospace throughout. A line marks its time (``log-time``: even
    figures in the page's font), the event is the only bold part, and only a raw error is code;
    the list has an id the shared "Full timestamps" toggle can name."""
    from tow.log import log_event

    log_event("check_fail", topic_id="t1", error="qBittorrent: connection refused", error_class="qbit")
    page = BeautifulSoup(client.get("/history").text, "html.parser")
    box = page.select_one("#history-log.log-box")
    line = box.select_one(".log-line")
    assert line.select_one(".log-time > time[datetime]") is not None
    assert len(line.find_all("b", recursive=False)) == 1
    assert all(code.find_parent("details", class_="raw-error") for code in box.select("code"))


def test_history_has_one_full_timestamps_toggle_for_its_list(client):
    """1.32.1 C5: History's times are no tab stop each; one "Full timestamps" toggle beside the
    search names the list (app.js shows every time of it in full while pressed)."""
    page = BeautifulSoup(client.get("/history").text, "html.parser")
    toggles = page.select(".history-filter > button[data-full-times]")
    assert [b.get_text(strip=True) for b in toggles] == ["Полное время"]
    toggle = toggles[0]
    assert (toggle["data-full-times"], toggle["aria-controls"]) == ("history-log", "history-log")
    assert (toggle["type"], toggle["aria-pressed"], toggle["class"]) == ("button", "false", ["ghost", "sm"])
    assert page.select_one("#history-log.log-box") is not None
