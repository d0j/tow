"""The password reminder: never the password in disguise, and no PBKDF2 for an unchanged one."""

from __future__ import annotations

import contextlib

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


@contextlib.contextmanager
def refused(code):
    with pytest.raises(AuthConfigurationError) as caught:
        yield
    assert caught.value.code == code


def test_an_unchanged_reminder_is_not_checked_again(monkeypatch):
    record = lan_password_record("old-horse-battery", "a long reminder with several words in it")
    monkeypatch.setattr(auth, "lan_password_matches", lambda *_a: pytest.fail("no PBKDF2 for an unchanged reminder"))

    assert with_hint(record, "a long   reminder with several words in it ") == record


def test_a_reminder_that_is_the_password_without_its_spaces_is_refused():
    record = lan_password_record("correcthorsebattery")

    with refused("auth.hint_reveals_password"):
        with_hint(record, "correct horse battery", password="correcthorsebattery")
    assert password_hint(with_hint(record, "the stable animal", password="correcthorsebattery")) == "the stable animal"


@pytest.mark.parametrize("hint", ["MY SECRET PASS", "it is mysecretpass!", "My Secret Pass word"])
def test_a_new_password_hidden_in_the_reminder_is_refused(hint):
    with pytest.raises(AuthConfigurationError):
        clean_hint(hint, password="my secret pass")


@pytest.mark.parametrize("hint", ["correct horse tow", "Correct.Horse.Tow!", "it is correct_horse_tow", "horse-tow"])
def test_punctuation_or_a_part_does_not_hide_a_new_password(hint):
    with pytest.raises(AuthConfigurationError):
        clean_hint(hint, password="correct-horse-tow")


# Reminders that gave the password away (case, punctuation, word order, a fragment, leet).
REVEALING = [
    ("mydog2024!", "mydog202 and a 4"),
    ("mydog2024!", "dog2024"),
    ("mydog2024!", "m-y-d-o-g 2-0-2-4"),
    ("Correct-Horse-Tow", "correct horse tow"),
    ("Correct-Horse-Tow", "horse tow correct"),
    ("Correct-Horse-Tow", "first: correct, then horse, last tow"),
    ("tr0ub4dor&3", "troubador 3"),
    ("Пароль-2024", "пароль 2024"),
    ("Correct-Horse-Tow", "wot esroh"),  # backwards
]


@pytest.mark.parametrize(("password", "hint"), REVEALING)
def test_a_reminder_sharing_four_characters_in_a_row_with_the_password_is_refused(password, hint):
    with refused("auth.hint_reveals_password"):
        clean_hint(hint, password=password)
    with refused("auth.hint_reveals_password"):  # only the reminder changes
        with_hint(lan_password_record(password), hint, password=password)


def test_a_reminder_that_shares_nothing_is_kept():
    assert clean_hint("  the  stable animal ", password="Correct-Horse-Tow") == "the stable animal"
    assert clean_hint("our first dog", password="mydog2024!") == "our first dog"  # "dog" alone is three


def test_a_new_reminder_alone_needs_the_current_password_and_removing_it_does_not():
    record = lan_password_record("correct-horse-tow", "the stable animal")

    with refused("auth.hint_needs_password"):
        with_hint(record, "a new reminder")
    assert password_hint(with_hint(record, "")) == ""


def test_saving_settings_with_the_prefilled_reminder_changes_nothing():
    record = lan_password_record("old-horse-battery", "лошадь")
    save_secrets({"lan_auth": record})

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/password", data={"hint": "лошадь"}, follow_redirects=False
    )

    assert t("web.common.no_changes", "ru") in shown(response.headers["location"])
    assert load_secrets()["lan_auth"] == record
    assert "undo" not in load_state()
