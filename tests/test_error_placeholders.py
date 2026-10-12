"""A stored message never shows a raw ``{name}``: a record that lacks a value its text names (an
older TOW or another producer wrote it) is shown as the text stored with it, and without one the
value is left out together with what only framed it ("(error {status})" goes whole)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tow import errors
from tow.errors import Msg, TowError, render, render_stored, without_unfilled

TEXTS = {
    "en": {"auth": "the site asks to sign in", "auth_403": "the site asks to sign in (error 403)"},
    "ru": {"auth": "сайт просит войти", "auth_403": "сайт просит войти (ошибка 403)"},
}
NESTED_OUTER = {"en": "no connection: ", "ru": "нет связи: "}


def _nested(params: dict) -> dict:
    return {
        "code": "web.settings.ping_failed",
        "params": {"error": {"$msg": {"code": "mirrors.http_auth", "params": params}}},
    }


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_a_missing_value_falls_back_to_the_stored_text(lang):
    assert render_stored("mirrors.http_auth", {}, "Fake Site: HTTP 403", lang) == "Fake Site: HTTP 403"
    assert render_stored("mirrors.http_auth", None, "stored", lang) == "stored"
    assert render(_nested({}), lang, "stored") == "stored"  # missing deeper down: the same


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_without_a_stored_text_the_missing_value_goes_with_its_brackets(lang):
    assert render_stored("mirrors.http_auth", {}, "", lang) == TEXTS[lang]["auth"]
    assert TowError("mirrors.http_auth").text(lang) == TEXTS[lang]["auth"]
    assert Msg("mirrors.all_paused").text(lang).startswith(("all mirrors", "все зеркала"))  # "{tracker}: " gone
    nested = render(_nested({}), lang)
    assert nested == NESTED_OUTER[lang] + TEXTS[lang]["auth"]
    assert "{" not in nested


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_complete_and_extra_values_render_as_before(lang):
    assert render_stored("mirrors.http_auth", {"status": 403}, "stored", lang) == TEXTS[lang]["auth_403"]
    assert render_stored("mirrors.http_auth", {"status": 403, "extra": "x"}, "stored", lang) == TEXTS[lang]["auth_403"]
    assert render(_nested({"status": 403}), lang, "stored") == NESTED_OUTER[lang] + TEXTS[lang]["auth_403"]
    assert Msg("mirrors.all_paused", tracker="Fake Site").text(lang).startswith("Fake Site: ")
    assert render_stored("no.such.code", {}, "stored", lang) == "stored"  # unknown code: as before


def test_history_keeps_its_own_way_for_an_unknown_code_and_its_stored_text_for_a_missing_value():
    from tow.log import format_event

    missing = format_event(
        {"kind": "check_fail", "error_code": "mirrors.http_auth", "error_params": {}, "error": "HTTP 403"}
    )
    assert "{status}" not in missing["detail"]
    assert "HTTP 403" in missing["detail"]
    whole = format_event(
        {"kind": "check_fail", "error_code": "mirrors.http_auth", "error_params": {"status": 403}, "error": "x"}
    )
    assert "403" in whole["detail"]


@pytest.mark.parametrize(
    ("marked", "shown"),
    [
        ("asks to sign in (error \x00status\x00)", "asks to sign in"),
        ("\x00tracker\x00: all mirrors are paused", "all mirrors are paused"),
        ("client «\x00name\x00» is down", "client is down"),
        ("folder: \x00path\x00.", "folder."),
        ("no connection: \x00error\x00", "no connection"),
        ("[\x00x\x00] a , b", "a, b"),
    ],
)
def test_what_only_framed_a_missing_value_goes_with_it(marked, shown):
    assert without_unfilled(marked) == shown


def test_home_rows_the_edit_panel_and_the_history_show_no_raw_placeholder():
    from tow.log import log_event
    from tow.store import save_state
    from tow.web import app

    url = "https://nnmclub.to/forum/viewtopic.php?t="
    record = {"last_error_class": "tracker_auth", "last_error_code": "mirrors.http_auth"}
    save_state(
        {
            "topics": [
                {
                    "id": "a",
                    "title": "Show A",
                    "url": url + "1",
                    **record,
                    "last_error_params": {},
                    "last_error": "NNM: HTTP 403",
                },
                {
                    "id": "b",
                    "title": "Show B",
                    "url": url + "2",
                    **record,
                    "last_error_params": {"status": 403},
                    "last_error": "x",
                },
            ],
            "mirrors": {},
        }
    )
    log_event(
        "check_fail",
        title="Show A",
        error="NNM: HTTP 403",
        error_code="mirrors.http_auth",
        error_params={},
        cls="tracker_auth",
    )
    client = TestClient(app)
    home, panel, history = client.get("/").text, client.get("/topics/a/edit-panel").text, client.get("/history").text
    for page in (home, panel, history):
        assert "{status}" not in page
    assert "HTTP 403" in panel  # the stored text in full
    assert "сайт просит войти (ошибка 403)" in client.get("/topics/b/edit-panel").text
    assert "{status}" not in errors.render_stored("mirrors.http_auth", {}, "")
