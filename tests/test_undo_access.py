"""Undoing a password or network-access change (owner's decision: a signed-in device is the owner).

A device on the network may undo a password change; the protection is explicit: the password
that comes back moves the session epoch, so no session from before the undo is valid again, and
the message says what came back. Network access itself is switched only on the computer running
TOW, so an undo that would switch it is refused from the network.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from helpers import flash_kind, flash_of

from tow import auth, undo
from tow.auth import issue_session, lan_password_record, lan_password_session_key, password_hint, session_is_valid
from tow.config import load_config, save_config
from tow.store import load_secrets, load_state, save_secrets
from tow.web import app

LAN = ("192.168.1.7", 50000)
ORIGIN = {"Origin": "http://127.0.0.1"}
OLD, NEW = "old-horse-battery", "new-cat-garden"


def _with_password(password: str = OLD, *, hint: str = "", allow_lan: bool = True) -> dict:
    secrets = load_secrets()
    secrets["lan_auth"] = lan_password_record(password, hint)
    save_secrets(secrets)
    cfg = load_config()
    cfg.update(allow_lan=allow_lan, bind="0.0.0.0" if allow_lan else "127.0.0.1")
    save_config(cfg)
    return secrets["lan_auth"]


def _key(record: dict) -> str:
    return lan_password_session_key(record)


def _device(record: dict) -> TestClient:
    return TestClient(app, client=LAN, headers=ORIGIN, cookies={"tow_session": issue_session(_key(record))})


def _local() -> TestClient:
    return TestClient(app, headers=ORIGIN)


def _change_password(client: TestClient, password: str = NEW, hint: str = "") -> dict:
    data = {"current_password": OLD, "lan_password": password, "lan_password2": password, "hint": hint}
    response = client.post("/settings/password", data=data, follow_redirects=False)
    assert response.status_code == 303
    return load_secrets()["lan_auth"]


def test_a_device_on_the_network_may_undo_a_password_change_and_no_old_session_comes_back():
    old = _with_password()
    before_change = issue_session(_key(old))  # a phone signed in with the old password
    new = _change_password(_local())
    after_change = issue_session(_key(new))  # a laptop signed in with the new one
    owner = _device(new)
    epoch = auth._sessions_state()[0]

    response = owner.post("/undo", follow_redirects=False)

    location = response.headers["location"]
    assert flash_of(location) == (
        "смена пароля отменена: снова действует прежний пароль; все устройства входят заново с ним"
    )
    assert flash_kind(location) == "ok"
    assert load_secrets()["lan_auth"] == old  # the old password is back...
    assert auth._sessions_state()[0] == epoch + 1  # ...the session epoch moved on...
    assert not session_is_valid(before_change, _key(old))  # ...so the old sessions stay out
    assert not session_is_valid(after_change, _key(old))
    cookie = response.cookies.get("tow_session")
    assert cookie
    assert session_is_valid(cookie, _key(old))  # the device that undid it stays signed in
    assert "undo" not in load_state()


def test_an_undo_of_a_password_change_on_this_pc_signs_every_device_out():
    old = _with_password()
    new = _change_password(_local())
    device = issue_session(_key(new))

    response = _local().post("/undo", follow_redirects=False)

    assert "прежний пароль" in flash_of(response.headers["location"])
    assert "tow_session" not in response.cookies  # this PC needs no session
    assert load_secrets()["lan_auth"] == old
    assert not session_is_valid(device, _key(old))
    assert not session_is_valid(device, _key(new))


def test_undoing_a_reminder_change_keeps_everyone_signed_in():
    record = _with_password(hint="старая")
    device = issue_session(_key(record))
    _local().post("/settings/password", data={"current_password": OLD, "hint": "новая"})
    epoch = auth._sessions_state()[0]

    response = _local().post("/undo", follow_redirects=False)

    assert flash_of(response.headers["location"]) == (
        "изменение подсказки отменено: вернулась прежняя подсказка; пароль тот же"
    )
    assert password_hint(load_secrets()["lan_auth"]) == "старая"
    assert auth._sessions_state()[0] == epoch
    assert session_is_valid(device, _key(record))


def test_the_network_cannot_undo_switching_network_access_on():
    record = _with_password(allow_lan=False)
    _local().post("/settings/access", data={"allow_lan": "1"})
    assert load_config()["allow_lan"] is True

    response = _device(record).post("/undo", follow_redirects=False)

    location = response.headers["location"]
    assert flash_of(location) == (
        "доступ по сети включается и выключается только на компьютере с TOW: отмените это изменение там"
    )
    assert flash_kind(location) == "err"
    assert load_config()["allow_lan"] is True  # nothing changed...
    assert load_state()["undo"]["kind"] == "settings_access"  # ...and this PC can still undo it
    _local().post("/undo")
    assert load_config()["allow_lan"] is False


def test_the_network_cannot_undo_the_password_away_while_the_network_is_open():
    # The first password, set on this PC; the network opened from config.yaml since.
    _local().post("/settings/password", data={"lan_password": OLD, "lan_password2": OLD})
    record = load_secrets()["lan_auth"]
    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    state = load_state()
    state["undo"].update(old_allow_lan=True, old_bind="0.0.0.0")
    from tow.store import save_state

    save_state(state)

    response = _device(record).post("/undo", follow_redirects=False)

    assert flash_kind(response.headers["location"]) == "err"
    assert load_secrets()["lan_auth"] == record


def test_the_undo_bar_names_a_password_change():
    _with_password()
    _change_password(_local())

    assert undo.undo_label() == "изменение пароля"


def test_an_undo_says_so_when_other_devices_could_not_be_signed_out(monkeypatch):
    old = _with_password()
    _change_password(_local())

    def sessions_file_locked():
        raise OSError("sessions.json in use")

    monkeypatch.setattr("tow.auth.sign_out_everywhere", sessions_file_locked)

    response = _local().post("/undo", follow_redirects=False)

    location = response.headers["location"]
    assert "другие устройства не удалось вывести" in flash_of(location)
    assert flash_kind(location) == "warn"
    assert load_secrets()["lan_auth"] == old  # the undo itself is done


def test_an_access_undo_written_before_1_19_still_works_and_its_lan_auth_flag_is_ignored():
    from tow.clock import iso_now
    from tow.paths import config_path
    from tow.store import save_state

    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    record = {"kind": "settings_access", "old_bind": "127.0.0.1", "old_allow_lan": False, "old_lan_auth": True}
    save_state({"topics": [], "mirrors": {}, "undo": {**record, "ts": iso_now()}})

    response = _local().post("/undo", follow_redirects=False)

    assert flash_of(response.headers["location"]) == "доступ восстановлен"
    cfg = load_config()
    assert (cfg["bind"], cfg["allow_lan"]) == ("127.0.0.1", False)
    assert "lan_auth" not in config_path().read_text(encoding="utf-8")
    assert "undo" not in load_state()
