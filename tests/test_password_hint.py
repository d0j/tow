"""The password reminder: never the password in disguise, and no PBKDF2 for an unchanged one."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from helpers import shown

from tow import auth
from tow.auth import AuthConfigurationError, clean_hint, lan_password_record, password_hint, with_hint
from tow.i18n import t
from tow.store import load_secrets, load_state, save_secrets
from tow.web import app


@pytest.fixture(autouse=True)
def _throwaway_master_key(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))


def test_an_unchanged_reminder_is_not_checked_again(monkeypatch):
    record = lan_password_record("old-horse-battery", "a long reminder with several words in it")
    monkeypatch.setattr(auth, "lan_password_matches", lambda *_a: pytest.fail("no PBKDF2 for an unchanged reminder"))

    assert with_hint(record, "a long   reminder with several words in it ") == record


def test_a_reminder_that_is_the_password_without_its_spaces_is_refused():
    record = lan_password_record("correcthorsebattery")

    with pytest.raises(AuthConfigurationError):
        with_hint(record, "correct horse battery")
    assert password_hint(with_hint(record, "the horse one")) == "the horse one"


@pytest.mark.parametrize("hint", ["MY SECRET PASS", "it is mysecretpass!", "My Secret Pass word"])
def test_a_new_password_hidden_in_the_reminder_is_refused(hint):
    with pytest.raises(AuthConfigurationError):
        clean_hint(hint, password="my secret pass")


def test_saving_settings_with_the_prefilled_reminder_changes_nothing():
    record = lan_password_record("old-horse-battery", "лошадь")
    save_secrets({"lan_auth": record})

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/password", data={"hint": "лошадь"}, follow_redirects=False
    )

    assert t("web.common.no_changes", "ru") in shown(response.headers["location"])
    assert load_secrets()["lan_auth"] == record
    assert "undo" not in load_state()
