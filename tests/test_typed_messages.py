"""Errors and TOW's own labels are stored as a code and values and shown in the reader's language.

Old records (text only, written by 1.17 and older) still show their stored text.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_check_contract import FakeClient, _wire_fake_check

from tow import check, check_steps, errors, i18n
from tow.config import load_config, save_config
from tow.errors import Msg, TowError
from tow.log import format_event, log_event
from tow.notify import NotificationBatch
from tow.store import load_state, save_download_history, save_state
from tow.trackers.generic import TrackerError
from tow.web import app

ORIGIN = {"Origin": "http://127.0.0.1"}
NO_LINK_RU = "rutor: на странице нет ссылки на торрент-файл — нужен вход на сайт"
NO_LINK_EN = "rutor: the page has no torrent link — sign in to the site"


def _browser(lang: str) -> TestClient:
    cfg = load_config()
    cfg["language"] = "auto"
    save_config(cfg)
    return TestClient(app, headers={**ORIGIN, "Accept-Language": lang})


def _topic(**extra) -> dict:
    return {
        "id": "t1",
        "title": "Show",
        "url": "http://rutor.info/torrent/101/show",
        "save_path": r"M:\anime",
        **extra,
    }


def test_a_failed_check_stores_the_code_values_and_class(monkeypatch):
    save_state({"topics": [_topic(hash=None)]})
    save_download_history({"schema_version": 1, "topics": {}})
    tracker = _wire_fake_check(monkeypatch, FakeClient())

    def refuse(*_args, **_kwargs):
        raise TrackerError("tracker.no_download_link", prefix="rutor")

    monkeypatch.setattr(tracker, "fetch_torrent", refuse)
    row = check.run_check(apply=True, notify=False, how="test")["results"][0]

    topic = load_state()["topics"][0]
    assert topic["last_error"] == NO_LINK_RU  # the text, for older versions
    assert topic["last_error_code"] == "tracker.no_download_link"
    assert topic["last_error_params"] == {"_prefix": "rutor"}
    assert topic["last_error_class"] == "tracker_auth"
    assert row["error_class"] == "tracker_auth"


def test_a_later_error_without_a_code_drops_the_old_code(monkeypatch):
    save_state({"topics": [_topic(hash=None, last_error="x", last_error_code="check.frozen", last_error_params={})]})
    save_download_history({"schema_version": 1, "topics": {}})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    monkeypatch.setattr(tracker, "fetch_torrent", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk gone")))
    check.run_check(apply=True, notify=False, how="test")
    topic = load_state()["topics"][0]
    assert topic["last_error"] == "disk gone"
    assert "last_error_code" not in topic
    assert "last_error_params" not in topic


def test_home_shows_the_error_in_the_readers_language():
    save_state(
        {
            "topics": [
                _topic(
                    last_error=NO_LINK_RU,
                    last_error_code="tracker.no_download_link",
                    last_error_params={"_prefix": "rutor"},
                    last_error_class="tracker_auth",
                )
            ]
        }
    )
    english = _browser("en").get("/").text
    assert "the page has no torrent link" in english
    assert "нет ссылки" not in english
    panel = _browser("en").get("/topics/t1/edit-panel").text
    assert NO_LINK_EN in panel
    russian = _browser("ru").get("/topics/t1/edit-panel").text
    assert NO_LINK_RU in russian


def test_an_old_error_without_a_code_shows_its_stored_text():
    save_state({"topics": [_topic(last_error="старый текст ошибки", last_error_class="error")]})
    assert "старый текст ошибки" in _browser("en").get("/topics/t1/edit-panel").text


def test_the_stop_previous_revision_offer_follows_the_code():
    blocked = _topic(hash="AB" * 20, last_error="x", last_error_code="check.previous_revision_active")
    blocked["last_error_params"] = {"file": "Show.S01E01.mkv"}
    legacy = _topic(id="t2", hash="CD" * 20, last_error="previous torrent revision is still active on …")
    other = _topic(id="t3", hash="EF" * 20, last_error="previous torrent revision", last_error_code="check.frozen")
    save_state({"topics": [blocked, legacy, other]})
    browser = _browser("en")
    assert "/topics/t1/replace-revision" in browser.get("/topics/t1/edit-panel").text
    assert "/topics/t2/replace-revision" in browser.get("/topics/t2/edit-panel").text
    assert "/topics/t3/replace-revision" not in browser.get("/topics/t3/edit-panel").text
    assert check.blocked_by_previous_revision(blocked)
    assert not check.blocked_by_previous_revision(other)


def test_history_renders_the_error_code_in_the_readers_language():
    log_event("check_fail", error=NO_LINK_RU, error_code="tracker.no_download_link", error_params={"_prefix": "rutor"})
    i18n.use("en")
    from tow.log import read_events

    row = format_event(read_events(limit=1)[0])
    assert NO_LINK_EN in row["detail"]
    english = _browser("en").get("/history").text
    assert NO_LINK_EN in english


def test_a_notification_speaks_the_owners_language():
    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    from tow.delivery import compose

    batch = NotificationBatch()
    batch.queue(_topic(), kind="error", operation_id="op", error=TowError("check.client_unreachable", reason="x"))
    batch.queue(_topic(), kind="error", operation_id="op", error=TowError("check.frozen"))
    text = compose(batch)[0][0]
    assert "client unreachable: x" in text
    assert "the site is paused" in text
    assert "клиент" not in text


def test_progress_labels_are_rendered_for_the_reader():
    from tow.web.views import _event_display

    event = {"kind": "client_restored", "label": "Торрент снова в клиенте", "label_code": "progress.back_in_client"}
    i18n.use("en")
    assert _event_display(event) == "Back in the client"
    i18n.use("ru")
    assert _event_display(event) == "Торрент снова в клиенте"
    assert _event_display({"kind": "new_file", "label": "Show.S01E01.mkv"}) == "Show.S01E01.mkv"


def test_episode_labels_carry_their_code():
    from tow.progress import _episode_keys_label_msg, _label_fields

    label = _episode_keys_label_msg({"episode:e5"})
    assert isinstance(label, Msg)
    fields = _label_fields(label)
    assert fields["label_code"] == "progress.episode"
    assert errors.render_stored(fields["label_code"], fields["label_params"], "", "en") == "Episode 05"
    assert _label_fields("S01E05") == {"label": "S01E05"}


def test_a_messenger_status_is_shown_in_the_readers_language():
    from tow.notifiers.outbox import set_status
    from tow.notifiers.view import _status_error

    state: dict = {}
    i18n.use("ru")
    set_status(state, "telegram", Msg("notifier.telegram.bad_token"))
    status = state["notify_status"]["telegram"]
    assert status["error_code"] == "notifier.telegram.bad_token"
    i18n.use("en")
    assert _status_error(status) == i18n.translate("notifier.telegram.bad_token", "en")
    assert _status_error({"error": "old text"}) == "old text"


def test_an_unconfirmed_add_is_recognised_by_its_code_also_inside_another_error():
    unconfirmed = TowError("client.managed.not_visible", prefix="Deluge")
    wrapped = TowError("check.reconcile_failed", error=unconfirmed)
    for error in (unconfirmed, wrapped):
        record = error.record()
        topic = {"last_error": "whatever", "last_error_code": record["code"], "last_error_params": record["params"]}
        assert check_steps.is_owned_add_recovery(topic)
    assert not check_steps.is_owned_add_recovery(
        {"last_error_code": "check.frozen", "last_error": "add не подтверждён"}
    )
    assert check_steps.is_owned_add_recovery({"last_error": "qBit ownership was not confirmed after add"})  # 1.17


def test_a_reconcile_failure_keeps_both_errors_typed():
    row = {"ok": False, "error": "rutor: x", "error_record": TowError("check.frozen").record()}
    check_steps.mark_reconcile_failure(row, TowError("progress.path_differs"))
    assert row["error_class"] == "no_path"
    assert row["error_record"]["code"] == "check.two_errors"
    english = errors.render(row["error_record"], "en")
    assert english.startswith("the site is paused; comparing with the torrent client failed: the client keeps")


def test_a_reconcile_failure_does_not_persist_secret_from_client_error():
    row = {"ok": True}
    check_steps.mark_reconcile_failure(row, RuntimeError("password=hunter2 in client reply"))
    assert "hunter2" not in str(row)
    assert "password=***" in row["error"]


def test_a_plural_of_a_number_too_large_for_a_float_is_the_other_form():
    # float(10**400) raised OverflowError out of every page that rendered such a stored value.
    assert i18n._category("ru", 10**400) == "other"
    assert i18n._category("ru", -(10**400)) == "other"
    assert i18n._category("ru", 21) == "one"
    stored = {"code": "notifier.common.dropped", "params": {"n": 10**400}}
    assert str(10**400) in errors.render(stored, "ru")


def test_messages_stored_inside_messages_are_rendered_to_a_bounded_depth():
    # A thousand stored levels (a damaged file) raised RecursionError from errors.render.
    code = "config_error.language_unread"
    words = i18n.t(code, "en", error="").strip()

    def nested(levels: int) -> dict:
        record: dict = {"code": "config_error.mapping", "params": {}}
        for _ in range(levels):
            record = {"code": code, "params": {"error": {"$msg": record}}}
        return record

    assert errors.render(nested(2), "en") == i18n.t(
        code, "en", error=i18n.t(code, "en", error=i18n.t("config_error.mapping", "en"))
    )
    deep = errors.render(nested(2000), "en")
    assert deep.count(words) == errors._MAX_NESTED_MESSAGES + 1
    assert deep.endswith(f": {code}")  # past the bound a stored message is shown as its code
    assert errors.render(nested(errors._MAX_NESTED_MESSAGES), "en").endswith(i18n.t("config_error.mapping", "en"))
