import pytest

from tow.notify import NotificationBatch, _episode_text, event_text, send, short_series_title


def test_notification_title_processing_ignores_unbounded_suffix():
    huge = "Show [S01E02 из 10]" + " " * 100_000 + "S99E99 secret suffix"
    assert short_series_title(huge) == "Show"
    assert _episode_text(huge, "ru") == "S01E02 из 10"


def test_notification_errors_do_not_send_secret_values():
    message = event_text(
        title="Show",
        kind="error",
        error="password=hunter2; request https://user:pass@example.test/?token=abc123",
    )
    assert "hunter2" not in message
    assert "abc123" not in message
    assert "user:pass" not in message


def test_send_no_token_silent():
    send({}, "x")
    send({"telegram": {"token": "", "chat_ids": [1]}}, "x")


def test_mirror_failure_notification_names_the_topic_and_does_not_repeat_mirrors():
    from tow.errors import Msg, TowError

    reason = TowError("mirrors.rate_limited")
    error = TowError(
        "mirrors.all_failed",
        tracker="Site A",
        error=reason,
        details=Msg("mirrors.mirror_reason", host="a.example", error=reason),
    )
    message = event_text(title="Show A / Alternate title", kind="error", error=error)
    assert "Show A" in message
    assert "Alternate title" not in message
    assert "a.example" not in message
    assert message.count("доступ к сайту временно ограничен") == 1


def test_no_messenger_is_no_working_notifications():
    from tow.notifiers import health

    assert health({}, {}) is not True
    assert health({"telegram": {}}, {}) is not True


def test_notification_batch_keeps_highest_priority_per_topic():
    topic = {"id": "topic-1", "title": "Show [01x01-05 из 5]"}
    batch = NotificationBatch()

    batch.queue(topic, kind="updated", operation_id="op-update", tracker="rutor")
    for index in range(5):
        batch.queue(topic, kind="new_file", operation_id=f"op-file-{index}", tracker="rutor")

    items = list(batch)
    assert len(items) == 1
    assert items[0].kind == "new_file"
    assert items[0].operation_id == "op-file-0"


def test_notification_batch_prefers_exact_episode_label_on_priority_tie():
    topic = {"id": "topic-1", "title": "Show"}
    batch = NotificationBatch()
    batch.queue(topic, kind="completed", operation_id="file", episodes="")
    batch.queue(topic, kind="completed", operation_id="episode", episodes="S01E03")
    assert next(iter(batch)).operation_id == "episode"


def test_notification_batch_combines_file_and_episode_labels_on_tie():
    topic = {"id": "topic-1", "title": "Show"}
    batch = NotificationBatch()
    batch.queue(topic, kind="completed", operation_id="file", episodes="Making of.mkv")
    batch.queue(topic, kind="completed", operation_id="episode", episodes="S01E05")
    assert next(iter(batch)).episodes == "Making of.mkv, S01E05"


def test_event_text():
    assert event_text(title="Сериал Д", kind="added") == "Сериал Д — добавлено в торрент-клиент"
    assert "новая версия добавлена" in event_text(title="X", kind="updated")
    assert event_text(title="", kind="qbit_down") == "Торрент-клиент недоступен"
    assert event_text(title="A", kind="error", error="лимит скачиваний на сегодня") == (
        "Сбой — A: лимит скачиваний на сегодня"
    )
    assert event_text(title="Show", kind="restored") == "Show — торрент снова в торрент-клиенте"


def test_completed_event_has_exact_status_and_range_total():
    title = "Сериал Г / Show G [04x01-22 из 24] (2026)"
    assert event_text(title=title, kind="completed", tracker="rutor") == (
        "Сериал Г — Rutor — S04E01–22 из 24 — загрузка завершена"
    )


def test_error_after_a_client_add_keeps_the_add_notification():
    from tow.notify import NotificationBatch, event_text

    batch = NotificationBatch()
    topic = {"id": "t1", "title": "Сериал А [S01E05 из 12]"}
    batch.queue(topic, kind="updated", operation_id="op1", episodes="S01E05")
    batch.queue(topic, kind="error", operation_id="op2", error="client save_path differs")

    items = list(batch)
    assert [item.kind for item in items] == ["updated"]
    text = event_text(title=topic["title"], kind="updated", error=items[0].error, episodes=items[0].episodes)
    assert "новая версия добавлена" in text
    assert "затем сбой: client save_path differs" in text


@pytest.mark.parametrize("event", ["new_file", "completed", "revision", "removed", "restored"])
@pytest.mark.parametrize("error_first", [False, True])
def test_an_error_never_hides_an_event_of_the_same_topic(event, error_first):
    # B2: an error used to outrank (and silently drop) completed/new_file/revision/removed.
    batch = NotificationBatch()
    topic = {"id": "t2", "title": "Show"}
    queued = [
        lambda: batch.queue(topic, kind=event, operation_id="op1", episodes="S01E02"),
        lambda: batch.queue(topic, kind="error", operation_id="op2", error="boom"),
    ]
    for queue in reversed(queued) if error_first else queued:
        queue()

    items = list(batch)
    assert [(item.kind, item.error, item.episodes) for item in items] == [(event, "boom", "S01E02")]
    text = event_text(title="Show", kind=event, error=items[0].error, episodes=items[0].episodes)
    assert text.endswith("; сбой: boom")


def test_a_higher_event_keeps_the_error_carried_by_a_lower_one():
    batch = NotificationBatch()
    topic = {"id": "t3", "title": "Show"}
    batch.queue(topic, kind="new_file", operation_id="op1")
    batch.queue(topic, kind="error", operation_id="op2", error="boom")
    batch.queue(topic, kind="completed", operation_id="op3")

    assert [(item.kind, item.error) for item in batch] == [("completed", "boom")]


def test_recovery_texts():
    assert event_text(title="Show [S01E05 из 12]", kind="recovered") == "Show — снова работает"
    assert event_text(title="", kind="qbit_up") == "Торрент-клиент снова доступен"


def test_recovered_is_kept_when_the_topic_also_has_an_event():
    from tow.delivery import compose
    from tow.notify import NotificationBatch

    for order in (("recovered", "added"), ("added", "recovered")):
        batch = NotificationBatch()
        topic = {"id": "t", "title": "Show"}
        for kind in order:
            batch.queue(topic, kind=kind, operation_id="op")
        ((text, _, _),) = compose(batch)
        assert text.startswith("Show — добавлено в торрент-клиент")
        assert text.endswith("(сбой устранён)")


def test_works_again_said_with_an_event_survives_a_later_event_of_the_topic():
    # "works again" folded into an add was lost when a completion of the same topic followed.
    topic = {"id": "topic-1", "title": "Show"}
    batch = NotificationBatch()
    batch.queue(topic, kind="recovered", operation_id="op-r", tracker="rutor")
    batch.queue(topic, kind="added", operation_id="op-a", tracker="rutor")
    batch.queue(topic, kind="completed", operation_id="op-c", tracker="rutor", episodes="S01E01")
    (item,) = list(batch)
    assert (item.kind, item.recovered) == ("completed", True)
    batch.queue(topic, kind="new_file", operation_id="op-f", tracker="rutor", error="client refused")
    (item,) = list(batch)
    assert (item.kind, item.recovered) == ("completed", True)


@pytest.mark.parametrize(
    ("title", "episodes"),
    [
        # "2x2" is a voice-over studio, not season 2 episode 2.
        ("Шоу / Show / Сезон: 1 / Серии: 1-8 из 10 (Режиссёр) [2024, WEB-DL 1080p] MVO (2x2)", "S01E01–08 из 10"),
        ("Шоу / Show [Сезон 1: 1-8 серии из 8] (2x2) [2021]", "S01E01–08 из 8"),
        # The usual RuTracker form, read as tow.episodes reads it for the topic's progress.
        ("Шоу / Show / Сезон: 2 / Серии: 1-12 из 12 [2023, WEBRip 1080p]", "S02E01–12 из 12"),
        ("Шоу (сезон 1 из 16, серии 1-20 из 20)", "S01E01–20 из 20"),
        # Controls.
        ("Show (1x01-05 из 10)", "S01E01–05 из 10"),
        ("Show [03x01-13 из 14]", "S03E01–13 из 14"),
        ("Show [S01E02 из 10]", "S01E02 из 10"),
        ("Show MVO (2x2)", ""),
        ("Film (2004)", ""),
        ("Show [1-8 из 10]", ""),  # no season named: nothing invented
    ],
)
def test_the_message_names_the_episodes_the_title_names(title, episodes):
    # The title of an added or updated topic said "S02E02 из 10" for "(2x2)" and nothing for the
    # usual "Сезон: 2 / Серии: 1-12 из 12".
    assert _episode_text(title, "ru") == episodes
