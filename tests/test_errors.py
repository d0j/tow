"""Typed errors: a catalog key and values, rendered in the reader's language; a class from the code."""

from __future__ import annotations

import json
import pickle

import pytest

from tow import errors, i18n
from tow.errors import Msg, TowError


def test_an_error_is_its_text_in_the_current_language():
    error = TowError("check.client_unreachable", reason="timeout")
    i18n.use("en")
    assert str(error) == "client unreachable: timeout"
    i18n.use("ru")
    assert str(error) == "клиент недоступен: timeout"
    assert error.text("en") == "client unreachable: timeout"


def test_the_record_is_json_and_renders_in_any_language():
    inner = TowError("check.no_connection")
    error = TowError("check.client_unreachable", reason=inner)
    record = json.loads(json.dumps(error.record()))
    assert record["code"] == "check.client_unreachable"
    assert record["cls"] == "qbit"
    assert errors.render(record, "en") == "client unreachable: no connection"
    assert errors.render(record, "ru") == "клиент недоступен: нет подключения"
    assert list(errors.codes_in(record)) == ["check.client_unreachable", "check.no_connection"]


def test_an_unknown_or_missing_record_falls_back_to_the_stored_text():
    assert errors.render({"code": "some.future.key", "params": {}}, "en", "old text") == "old text"
    assert errors.render(None, "en", "old text") == "old text"
    assert errors.render_stored("", {}, "old text") == "old text"
    assert errors.render_stored("check.no_connection", None, "x", "en") == "no connection"


def test_fractions_follow_the_readers_decimal_sign():
    error = TowError("check.low_disk", needed=1.5, free=0.25, path="D:\\")
    assert "1.5" in error.text("en")
    assert "1,5" in error.text("ru")
    assert "0,2" in error.text("ru")  # one decimal
    record = error.record()
    assert record["params"]["needed"] == 1.5  # kept as a number, formatted when shown


def test_a_prefix_names_who_reports_it():
    error = TowError("client.managed.missing", prefix="Transmission")
    assert error.text("en") == "Transmission: the torrent is not in the client"
    assert errors.render(error.record(), "ru") == "Transmission: раздачи нет в клиенте"


def test_the_class_comes_from_the_site_then_the_code_then_the_type():
    assert TowError("check.low_disk").error_class == "disk"
    assert TowError("client.anything.new").error_class == "qbit"  # a new client's section
    assert TowError("client.managed.wrong_folder").error_class == "no_path"  # longest match
    assert TowError("check.low_disk", cls="error").error_class == "error"

    class Special(TowError):
        default_class = "auth"

    assert Special("nowhere.listed").error_class == "auth"
    assert errors.class_of("nowhere.listed") is None


def test_a_messages_values_are_messages_too():
    message = Msg("check.client_unreachable", reason=Msg("check.no_connection"))
    assert message.text("en") == "client unreachable: no connection"
    assert errors.text_of(message.record(), "ru") == "клиент недоступен: нет подключения"
    assert errors.text_of("plain", "en") == "plain"


def test_record_of_finds_the_error_behind_a_plain_one():
    outer = RuntimeError("wrapped")
    outer.__cause__ = TowError("check.no_connection")
    assert errors.record_of(outer)["code"] == "check.no_connection"
    assert errors.record_of(ValueError("x")) is None


def test_an_error_survives_pickling():
    error = TowError("check.client_unreachable", reason="x", prefix="qBit")
    again = pickle.loads(pickle.dumps(error))
    assert (again.code, again.params, again.error_class) == (error.code, error.params, error.error_class)


@pytest.mark.parametrize("base", [ValueError, RuntimeError])
def test_typed_errors_keep_their_builtin_family(base):
    class Typed(TowError, base):
        pass

    with pytest.raises(base) as caught:
        raise Typed("check.no_connection")
    assert caught.value.code == "check.no_connection"


def test_every_class_in_the_table_is_a_status_class():
    assert set(errors.CLASSES.values()) <= set(errors.STATUS_CLASSES)
