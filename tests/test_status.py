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


def test_the_site_icon_is_green_when_the_site_answered_and_the_client_failed():
    # QA r5: a new topic whose site gave the .torrent but whose client step failed showed the
    # site icon grey, "not checked yet".
    at = "2026-10-08T20:00:00+03:00"
    topic = {"last_ok": False, "last_error": "клиент недоступен", "last_error_class": "qbit", "last_check": at}
    assert project_home_status(topic, {}).tracker_tone == "mut"
    assert project_home_status({**topic, "site_ok_at": at}, {}).tracker_tone == "ok"


def test_the_site_tone_of_a_check():
    from tow.status import site_check_tone

    at = "2026-10-08T20:00:00+03:00"
    assert site_check_tone({}) is None
    assert site_check_tone({"last_check": at, "site_ok_at": at, "last_ok": True}) == "ok"
    assert site_check_tone({"last_check": at, "last_error": "x", "last_error_class": "tracker"}) == "warn"
    assert site_check_tone({"last_check": at, "last_error": "x", "last_error_class": "tracker_auth"}) == "warn"
    assert site_check_tone({"last_check": at, "last_error": "x", "last_error_class": "gone"}) == "ok"
    # the client failed, the site was not asked in that check: says nothing about the site
    assert (
        site_check_tone({"last_check": at, "site_ok_at": "earlier", "last_error": "x", "last_error_class": "qbit"})
        is None
    )


def test_the_header_site_follows_the_latest_check_or_probe():
    # QA r5: the header's site names (and Sites) followed only Diagnostics probes: a site no
    # probe had asked stayed "not checked yet" after any number of checks.
    from tow.store import save_state
    from tow.web.templating import header_health

    url = "https://rutor.info/torrent/1"
    t1, t2, t3 = "2026-10-08T10:00:00+03:00", "2026-10-08T11:00:00+03:00", "2026-10-08T12:00:00+03:00"
    answered = {"id": "a", "url": url, "last_ok": True, "last_check": t2, "site_ok_at": t2}
    down = {"id": "a", "url": url, "last_ok": False, "last_check": t2, "last_error": "x", "last_error_class": "tracker"}
    probe = {"tracker": "rutor", "host": "https://rutor.info", "ok": True}

    def sites(topic, *probes):
        save_state({"topics": [topic], "doctor": {"probes": list(probes)}})
        return header_health()["sites"]

    assert sites(answered) == {"rutor": "ok"}  # no probe ever: the check says it answered
    assert sites(down, {**probe, "at": t1}) == {"rutor": "warn"}  # the check came later
    assert sites(down, {**probe, "at": t3}) == {"rutor": "ok"}  # the probe came later
    assert sites(down, probe) == {"rutor": "warn"}  # a probe without a time is older
    assert sites({"id": "a", "url": url}, {**probe, "ok": False}) == {"rutor": "warn"}  # probes alone
    assert sites({"id": "a", "url": url}) == {"rutor": "mut"}
