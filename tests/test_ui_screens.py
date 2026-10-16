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
    assert note == "Хранится зашифрованным, не показывается и не пишется в журнал."


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
    the field is then linked to the "leave empty to keep" hint; with none saved there is no hint."""
    from tow.store import load_secrets, save_secrets

    secrets = load_secrets()
    secrets.setdefault("trackers", {})["rutor"] = {"username": "fixture-user", **({"password": "x"} if stored else {})}
    save_secrets(secrets)
    row = BeautifulSoup(client.get("/sites?open=rutor").text, "html.parser").select_one("#ed-site-rutor")
    field = row.select_one("#site-pass-rutor")
    assert field.get("value") is None
    assert (row.select_one("#site-pass-saved-rutor") is not None) is stored
    assert field.get("aria-describedby") == ("site-pass-saved-rutor" if stored else None)


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
        assert field["placeholder"] == r"download\.php\?id=(\d+)"


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
    a sentence and the field it is about is marked."""
    _first_start()
    data = {"lan_password": "river-lamp-9", "lan_password2": "river-lamp-9", "hint": "river-lamp-9!", "allow_lan": "1"}
    refused = client.post("/setup", data=data)
    assert refused.status_code == 400
    page = BeautifulSoup(refused.text, "html.parser")
    assert page.select_one("#lan-hint")["value"] == "river-lamp-9!"
    assert page.select_one("#allow-lan").has_attr("checked")
    for field in ("#lan-password", "#lan-password2"):
        assert not page.select_one(field).has_attr("value")
    assert page.select_one("#lan-hint")["aria-invalid"] == "true"
    error = page.select_one("#setup-error").get_text()
    assert error[:1].isupper()
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
    for field in ("lan-password", "lan-password2"):
        assert page.select_one(f'button.reveal[data-reveal="{field}"]') is not None
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
