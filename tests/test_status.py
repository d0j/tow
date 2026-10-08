from tow.status import project_home_status


def test_successful_torrent_action_survives_tracker_failure():
    status = project_home_status(
        {
            "last_ok": False,
            "last_error": "rutor: все зеркала на паузе",
            "last_changed": False,
        },
        {
            "client_present": True,
            "last_event": {"kind": "episode_completed", "label": "S01E01"},
        },
    )

    assert status.torrent_tone == "warn"
    assert status.tracker_tone == "warn"
    assert status.tracker_label == "Сайт временно недоступен"
    assert status.torrent_action == "episode_completed"


def test_tracker_failure_without_successful_torrent_action_is_warning():
    status = project_home_status(
        {"last_ok": False, "last_error": "rutor: все зеркала на паузе"},
        {"items": {}},
    )

    assert status.torrent_tone == "warn"
    assert status.tracker_tone == "warn"


def test_client_failure_is_red_for_the_dot_but_not_the_site():
    # The site icon shows the site alone (AGENTS.md): a client error is not the site's fault.
    never_seen = project_home_status({"last_ok": False, "last_error": "qBittorrent timeout"}, {"items": {}})
    assert never_seen.torrent_tone == "bad"
    assert never_seen.tracker_tone == "mut"
    seen_before = project_home_status(
        {"last_ok": False, "last_error": "qBittorrent timeout", "last_ok_at": "01.10.2026 10:00:00"},
        {"items": {}},
    )
    assert seen_before.torrent_tone == "bad"
    assert seen_before.tracker_tone == "ok"


def test_a_removed_topic_is_red_on_the_site_icon():
    status = project_home_status({"last_ok": False, "last_error": "rutor: all hosts failed: http 404"}, {})
    assert status.torrent_tone == "bad"
    assert status.tracker_tone == "bad"
    assert status.tracker_label == "Проблема на сайте"


def test_tracker_check_failure_event_is_warning_not_internal_failure():
    status = project_home_status(
        {"last_ok": False, "last_error": "all hosts failed: network unavailable"},
        {"last_event": {"kind": "check_fail", "at": "2026-09-14T00:00:00+03:00"}},
    )

    assert status.torrent_tone == "warn"
    assert status.tracker_tone == "warn"


def test_unknown_topic_is_gray():
    status = project_home_status(
        {"last_ok": False, "last_error": None, "last_changed": False},
        {"items": {}},
    )

    assert status.torrent_tone == "mut"
    assert status.tracker_tone == "mut"


def test_new_tracker_action_is_blue_when_no_error():
    status = project_home_status(
        {"last_ok": True, "last_error": None, "last_changed": True},
        {"items": {}},
    )

    assert status.torrent_tone == "new"
    assert status.tracker_tone == "ok"


def test_old_client_event_does_not_override_current_check_state():
    status = project_home_status(
        {"last_ok": True, "last_changed": False, "last_error": None},
        {"last_event": {"kind": "client_added"}},
    )
    assert status.torrent_tone == "ok"

    unobserved = project_home_status(
        {"last_ok": False, "last_changed": False, "last_error": None},
        {"last_event": {"kind": "client_removed"}},
    )
    assert unobserved.torrent_tone == "mut"


def test_nnmclub_auth_failure_is_actionable_red_and_the_site_amber():
    # AGENTS.md: the dot is red for a sign-in, the site icon amber (the site wants a login).
    status = project_home_status(
        {"last_ok": False, "last_error": "nnmclub: no download link on page", "hash": None},
        {"items": {}},
    )

    assert status.torrent_tone == "bad"
    assert status.tracker_tone == "warn"
    assert status.tracker_label == "Сайт просит войти"
    assert status.torrent_label == "Ошибка — причина в строке, откройте её"
