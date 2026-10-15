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


@pytest.mark.parametrize("language", ["en", "ru"])
def test_legacy_mirror_failure_has_optional_diagnostics_not_incomplete(language):
    record = {
        "code": "mirrors.all_failed",
        "params": {
            "tracker": "Site A",
            "error": errors.as_value({"code": "mirrors.http_status", "params": {"status": 503}}),
        },
    }
    assert errors.render(record, language, "old raw text", incomplete="old raw text").startswith("Site A:")
    assert "details" not in record["params"]
    incomplete = {"code": "mirrors.all_failed", "params": {"error": record["params"]["error"]}}
    assert errors.render(incomplete, language, incomplete="required field missing") == "required field missing"


def test_fractions_follow_the_readers_decimal_sign():
    error = TowError("check.low_disk", needed=1.5, free=0.25, path="D:\\")
    assert "1.5" in error.text("en")
    assert "1,5" in error.text("ru")
    assert "0,2" in error.text("ru")  # one decimal
    record = error.record()
    assert record["params"]["needed"] == 1.5  # kept as a number, formatted when shown


@pytest.mark.parametrize("language", ["en", "ru"])
def test_mirror_failure_summary_keeps_diagnostics_separate_and_state_unchanged(language):
    reason = TowError("mirrors.rate_limited")
    details = Msg(
        "mirrors.reason_pair",
        first=Msg("mirrors.mirror_reason", host="a.example", error=reason),
        second=Msg("mirrors.mirror_reason", host="b.example", error=TowError("mirrors.cloudflare")),
    )
    error = TowError("mirrors.all_failed", cls="tracker", tracker="Site A", error=reason, details=details)
    record = error.record()
    before = json.dumps(record)
    summary = errors.render(record, language, "old verbose fallback")
    assert summary == f"Site A: {reason.text(language)}"
    assert "a.example" not in summary
    assert "b.example" not in summary
    expanded = errors.diagnostic_details(record, language)
    assert "a.example" in expanded
    assert "b.example" in expanded
    assert reason.text(language) in expanded
    assert TowError("mirrors.cloudflare").text(language) in expanded
    assert errors.text_of(error, language) == summary
    assert json.dumps(record) == before
    assert record["cls"] == "tracker"


@pytest.mark.parametrize(
    "record", [None, {}, {"code": "check.no_connection"}, {"code": "mirrors.all_failed", "params": None}]
)
def test_missing_mirror_diagnostics_do_not_invent_details(record):
    assert errors.diagnostic_details(record) == ""


def test_incomplete_hidden_diagnostics_cannot_restore_the_verbose_fallback():
    record = TowError("mirrors.all_failed", tracker="Site A", error=TowError("mirrors.rate_limited")).record()
    record["params"]["details"] = errors.as_value({"code": "mirrors.mirror_reason", "params": {"host": "a.example"}})
    assert errors.render(record, "ru", "old repeated raw text") == "Site A: " + TowError("mirrors.rate_limited").text(
        "ru"
    )


@pytest.mark.parametrize(("language", "words"), [("en", "Site A: no connection"), ("ru", "Site A: нет связи")])
def test_a_lost_connection_keeps_its_words_in_the_short_summary(language, words):
    # Every mirror failing to connect ("no local network" among them) read as "site access is
    # temporarily limited": the owner looked at the site instead of this computer's connection.
    lost = Msg("mirrors.transport", error="no local network")
    record = TowError("mirrors.all_failed", tracker="Site A", error=lost).record()
    assert errors.summary_record(record) == record
    assert errors.summary_of(errors.as_value(record), language).startswith(words)
    # A site that also refuses or limits access is still the short site incident.
    mixed = Msg("mirrors.reason_pair", first=lost, second=TowError("mirrors.rate_limited"))
    record = TowError("mirrors.all_failed", tracker="Site A", error=mixed).record()
    assert errors.summary_record(record) == {"code": "mirrors.access_retry", "params": {"tracker": "Site A"}}


def test_a_prefix_names_who_reports_it():
    error = TowError("client.managed.missing", prefix="Transmission")
    assert error.text("en") == "Transmission: the torrent is not in the client"
    assert errors.render(error.record(), "ru") == "Transmission: торрента нет в клиенте"


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
