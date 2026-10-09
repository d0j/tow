"""The TOW password: first start, change in Settings, reminder and reset of a forgotten one."""

import os
import re
import sys

import pytest
from fastapi.testclient import TestClient
from helpers import shown

from tow.auth import issue_session, lan_password_matches, lan_password_session_key, password_hint, session_is_valid
from tow.config import load_config, save_config
from tow.store import load_secrets, load_state, save_secrets
from tow.web import app

LAN = ("192.168.1.7", 50000)
ORIGIN = {"Origin": "http://127.0.0.1"}
HTML = {"Accept": "text/html"}


def _fresh_install():
    """config.yaml as a new install has it: no password, first start not passed."""
    cfg = load_config()
    cfg.pop("setup_done", None)
    save_config(cfg)


def _with_password(password="old-horse-battery", hint="", allow_lan=True):
    from tow.auth import lan_password_record

    secrets = load_secrets()
    secrets["lan_auth"] = lan_password_record(password, hint)
    save_secrets(secrets)
    cfg = load_config()
    cfg.update(allow_lan=allow_lan, bind="0.0.0.0" if allow_lan else "127.0.0.1")
    save_config(cfg)
    return secrets["lan_auth"]


def _lan_client(record):
    return TestClient(
        app, client=LAN, headers=ORIGIN, cookies={"tow_session": issue_session(lan_password_session_key(record))}
    )


def _flash(response):
    return shown(response.headers["location"])


def test_first_start_offers_the_password_and_this_pc_sets_it():
    _fresh_install()
    local = TestClient(app, headers=ORIGIN)

    first = local.get("/", headers=HTML, follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/setup"
    page = local.get("/setup").text
    assert "Добро пожаловать в TOW" in page
    assert 'name="lan_password2"' in page
    assert 'name="hint"' in page
    assert 'value="skip"' in page

    mismatch = local.post("/setup", data={"lan_password": "first-pass-1", "lan_password2": "other-pass-2"})
    assert mismatch.status_code == 400
    assert "пароли не совпадают" in mismatch.text

    saved = local.post(
        "/setup",
        data={
            "lan_password": "pony-river-lamp",
            "lan_password2": "pony-river-lamp",
            "hint": "поняша",
            "allow_lan": "1",
        },
        follow_redirects=False,
    )
    assert "доступ с других устройств заработает после перезапуска" in _flash(saved)
    record = load_secrets()["lan_auth"]
    assert lan_password_matches("pony-river-lamp", record)
    assert password_hint(record) == "поняша"
    cfg = load_config()
    assert cfg["setup_done"] is True
    assert cfg["allow_lan"] is True
    assert cfg["bind"] == "0.0.0.0"
    assert local.get("/", headers=HTML, follow_redirects=False).status_code == 200
    assert local.get("/setup", follow_redirects=False).headers["location"] == "/"


def test_a_second_first_start_window_is_told_its_password_was_not_saved():
    _fresh_install()
    first, second = TestClient(app, headers=ORIGIN), TestClient(app, headers=ORIGIN)
    assert "Добро пожаловать в TOW" in first.get("/setup").text
    assert "Добро пожаловать в TOW" in second.get("/setup").text  # both opened before either saved
    data = {"lan_password": "pony-river-lamp", "lan_password2": "pony-river-lamp"}
    first.post("/setup", data=data, follow_redirects=False)

    late = second.post(
        "/setup", data={"lan_password": "other-pass-99", "lan_password2": "other-pass-99"}, follow_redirects=False
    )

    assert late.headers["location"].startswith("/settings?open=access")
    assert "пароль уже задан в другом окне; этот не сохранён" in _flash(late)
    assert lan_password_matches("pony-river-lamp", load_secrets()["lan_auth"])


def test_first_start_can_be_put_off():
    _fresh_install()
    local = TestClient(app, headers=ORIGIN)

    skipped = local.post("/setup", data={"action": "skip"}, follow_redirects=False)

    assert "пароль можно задать позже" in _flash(skipped)
    assert load_config()["setup_done"] is True
    assert "lan_auth" not in load_secrets()
    assert local.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_an_existing_password_means_no_first_start_page():
    _fresh_install()
    _with_password(allow_lan=False)

    assert TestClient(app).get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_another_device_cannot_use_the_first_start_page():
    record = _with_password()
    lan = _lan_client(record)
    cfg = load_config()
    cfg.pop("setup_done", None)
    save_config(cfg)

    response = lan.post(
        "/setup", data={"lan_password": "evil-pass-123", "lan_password2": "evil-pass-123"}, follow_redirects=False
    )

    assert response.headers["location"] == "/"
    assert lan_password_matches("old-horse-battery", load_secrets()["lan_auth"])


def test_the_reminder_never_reveals_the_password():
    local = TestClient(app, headers=ORIGIN)
    in_new = local.post(
        "/settings/password",
        data={"lan_password": "secret-word-1", "lan_password2": "secret-word-1", "hint": "it is SECRET-word-1 ok"},
        follow_redirects=False,
    )
    assert "подсказка не должна содержать пароль" in _flash(in_new)
    assert "lan_auth" not in load_secrets()

    _with_password("old-horse-battery")
    for current in ("", "old-horse-battery"):
        only_hint = local.post(
            "/settings/password",
            data={"current_password": current, "hint": "battery horse, old"},
            follow_redirects=False,
        )
        expected = "подсказка не должна содержать пароль" if current else "введите текущий пароль"
        assert expected in _flash(only_hint)
        assert password_hint(load_secrets()["lan_auth"]) == ""
    wrong = local.post(
        "/settings/password", data={"current_password": "guess-guess", "hint": "x"}, follow_redirects=False
    )
    assert "текущий пароль неверный" in _flash(wrong)

    too_long = local.post("/settings/password", data={"hint": "x" * 121}, follow_redirects=False)
    assert "не длиннее 120" in _flash(too_long)


def test_this_pc_resets_a_forgotten_password_without_the_old_one():
    old = _with_password("old-horse-battery", hint="old")
    other_device = issue_session(lan_password_session_key(old))
    local = TestClient(app, headers=ORIGIN)

    response = local.post(
        "/settings/password",
        data={"lan_password": "new-cat-garden", "lan_password2": "new-cat-garden", "hint": "кот в саду"},
        follow_redirects=False,
    )

    assert "пароль сохранён; другие устройства входят заново" in _flash(response)
    record = load_secrets()["lan_auth"]
    assert lan_password_matches("new-cat-garden", record)
    assert password_hint(record) == "кот в саду"
    assert not session_is_valid(other_device, lan_password_session_key(record))  # every device signs in again


def test_only_the_reminder_changes_and_sessions_stay():
    old = _with_password("old-horse-battery", hint="old")
    session = issue_session(lan_password_session_key(old))

    local = TestClient(app, headers=ORIGIN)
    assert 'name="current_password"' in local.get("/settings").text  # optional here: for the reminder alone
    local.post("/settings/password", data={"current_password": "old-horse-battery", "hint": "new reminder"})

    record = load_secrets()["lan_auth"]
    assert lan_password_matches("old-horse-battery", record)
    assert password_hint(record) == "new reminder"
    assert session_is_valid(session, lan_password_session_key(record))


def test_another_device_cannot_slip_the_password_into_the_reminder():
    record = _with_password("Correct-Horse-Tow")
    lan = _lan_client(record)

    for hint in ("correct horse tow", "horse tow correct"):
        response = lan.post(
            "/settings/password",
            data={"current_password": "Correct-Horse-Tow", "hint": hint},
            follow_redirects=False,
        )
        assert "подсказка не должна содержать пароль" in _flash(response)
    assert password_hint(load_secrets()["lan_auth"]) == ""


def test_another_device_needs_the_current_password_and_stays_signed_in():
    record = _with_password("old-horse-battery")
    lan = _lan_client(record)
    page = lan.get("/settings").text
    assert 'name="current_password"' in page
    assert 'action="/settings/access"' not in page

    wrong = lan.post(
        "/settings/password",
        data={"current_password": "guess-guess", "lan_password": "new-cat-garden", "lan_password2": "new-cat-garden"},
        follow_redirects=False,
    )
    assert "текущий пароль неверный" in _flash(wrong)
    assert lan_password_matches("old-horse-battery", load_secrets()["lan_auth"])

    right = lan.post(
        "/settings/password",
        data={
            "current_password": "old-horse-battery",
            "lan_password": "new-cat-garden",
            "lan_password2": "new-cat-garden",
        },
        follow_redirects=False,
    )
    new_record = load_secrets()["lan_auth"]
    assert lan_password_matches("new-cat-garden", new_record)
    assert session_is_valid(right.cookies.get("tow_session"), lan_password_session_key(new_record))


def test_sign_in_page_shows_the_reminder_and_how_to_reset():
    _with_password("old-horse-battery", hint="лошадь")

    page = TestClient(app, client=LAN).get("/login").text

    assert "Забыли пароль?" in page
    assert "<b>лошадь</b>" in page
    assert "старый там не нужен" in page
    assert "http://127.0.0.1:8787" in page
    assert ("scripts\\tow.cmd password" if sys.platform == "win32" else "scripts/tow password") in page


def test_the_sign_in_page_puts_the_cursor_in_the_password_field():
    """Qa8: the sign-in page (nothing else to do there) opened with the focus nowhere; after a
    wrong password too. The password field takes it."""
    _with_password("old-horse-battery")
    lan = TestClient(app, client=LAN, headers=ORIGIN)
    assert re.search(r'<input id="lan-password"[^>]*\bautofocus\b', lan.get("/login").text)
    wrong = lan.post("/login", data={"password": "wrong-password-1"}, follow_redirects=False)
    assert re.search(r'<input id="lan-password"[^>]*\bautofocus\b', wrong.text)


def test_password_change_can_be_undone():
    _with_password("old-horse-battery", hint="old")
    local = TestClient(app, headers=ORIGIN)
    local.post("/settings/password", data={"lan_password": "new-cat-garden", "lan_password2": "new-cat-garden"})
    assert load_state()["undo"]["kind"] == "settings_access"

    local.post("/undo")

    record = load_secrets()["lan_auth"]
    assert lan_password_matches("old-horse-battery", record)
    assert password_hint(record) == "old"


def test_command_line_resets_the_password(monkeypatch, capsys):
    from tow import cli

    _with_password("old-horse-battery", allow_lan=False)
    answers = iter(["cli-pass-word", "cli-pass-word"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(answers))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "из консоли")

    assert cli.main(["password"]) == 0

    record = load_secrets()["lan_auth"]
    assert lan_password_matches("cli-pass-word", record)
    assert password_hint(record) == "из консоли"
    assert "cli-pass-word" not in capsys.readouterr().out
    assert os.environ.get("TOW_HOME")  # the test install, never the live one


def test_command_line_refuses_different_passwords(monkeypatch):
    from tow import cli

    answers = iter(["cli-pass-word", "cli-pass-other"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(answers))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    assert cli.main(["password"]) == 3
    assert "lan_auth" not in load_secrets()


@pytest.mark.parametrize("command", [["password"], ["access", "on"]])
def test_command_line_refuses_a_short_password_before_the_reminder(monkeypatch, capsys, command):
    # The reminder was asked for a password that was then refused as too short.
    from tow import cli
    from tow.i18n import t

    monkeypatch.setattr("getpass.getpass", lambda _prompt="": "short")
    reminders = []
    monkeypatch.setattr("builtins.input", lambda prompt="": reminders.append(prompt) or "")

    assert cli.main(command) == 3
    assert reminders == []
    assert t("auth.password_too_short", n=8) in capsys.readouterr().out
    assert "lan_auth" not in load_secrets()


def test_the_reset_command_is_written_the_way_this_system_runs_it():
    from tow import platform
    from tow.platform.posix import PosixBackend
    from tow.platform.windows import WindowsBackend
    from tow.web.templating import TEMPLATES

    launcher = TEMPLATES.env.globals["os_launcher"]
    with platform.use(WindowsBackend()):
        assert launcher() == "scripts\\tow.cmd"
    with platform.use(PosixBackend("linux")):
        assert launcher() == "scripts/tow"
        assert TEMPLATES.env.globals["os_sep"]() == "/"
        assert TEMPLATES.env.globals["os_example_folder"]() == "/srv/media"


def test_in_a_runtime_install_the_reset_command_names_the_app_folder(monkeypatch, tmp_path):
    from tow import paths, platform
    from tow.platform.posix import PosixBackend
    from tow.platform.windows import WindowsBackend
    from tow.web.templating import TEMPLATES

    monkeypatch.setenv("TOW_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path / "app")
    launcher = TEMPLATES.env.globals["os_launcher"]
    with platform.use(WindowsBackend()):
        assert launcher() == "app\\scripts\\tow.cmd"
    with platform.use(PosixBackend("linux")):
        assert launcher() == "app/scripts/tow"
