"""tow.issues: each kind from synthetic snapshots, the order and merge of ``collect``, the
fingerprint, and a broken kind that never hides the others."""

from __future__ import annotations

import importlib
import logging
import types

import pytest

from tow import issues, watchdog
from tow.issues import Fix, Issue, Message, Snapshot, Target, collect, fingerprint
from tow.issues.kinds import check_stale, data_lost

collect_module = importlib.import_module("tow.issues.collect")  # the package names its function collect

NOW = 1_800_000_000.0
NNM = "https://nnmclub.to/forum/viewtopic.php?t="
RUTOR = "https://rutor.info/torrent/"
CONFIG = {
    "interval_sec": 3600,
    "clients": {"nas": {"kind": "qbittorrent", "default": True}},
    "trackers": {
        "nnmclub": {
            "title": "NNM-Club",
            "url_regex": r"^https?://nnmclub\.to/forum/viewtopic\.php\?t=(\d+)",
            "fetch_hosts": ["https://nnmclub.to"],
            "login_path": "/forum/login.php",
        },
        "rutor": {"url_regex": r"^https?://rutor\.info/torrent/(\d+)", "fetch_hosts": ["https://rutor.info"]},
    },
}


def _snapshot(topics=(), health=None, **facts) -> Snapshot:
    return Snapshot(state={"topics": list(topics), "health": health or {}}, config=CONFIG, now=NOW, **facts)


def _auth(tid: str, url: str, **extra) -> dict:
    return {"id": tid, "url": url, "last_error_class": "tracker_auth", "last_error": "x", **extra}


def _kinds(snapshot: Snapshot) -> list[str]:
    return [issue.kind for issue in collect(snapshot)]


def test_every_kind_is_found_once_in_its_order():
    assert [kind.name for kind in issues.known_kinds()] == [
        "data_lost",
        "check_stale",
        "check_blocked",
        "client_down",
        "volume_missing",
        "history_rebuilt",
        "history_trimmed",
        "site_needs_login",
        "topic_error",
    ]
    assert issues.SKIPPED == []


def test_nothing_when_all_is_well():
    snapshot = _snapshot([{"id": "a", "url": NNM + "1"}], {"qbit_ok": True, "check_ok": True}, next_check_at=NOW + 60)
    assert collect(snapshot) == []
    assert fingerprint([]) == ""
    assert issues.worst([]) == ""


def test_a_lost_data_folder_counts_only_while_no_topic_is_watched():
    lost = {"at": 1, "folder": "D:\\TOW\\data"}
    [issue] = collect(_snapshot(data_lost=lost))
    assert (issue.kind, issue.severity, issue.message) == (
        "data_lost",
        "bad",
        Message("issues.data_lost", {"folder": "D:\\TOW\\data"}),
    )
    assert issue.target == Target("settings", open="transfer", anchor="backup-night", note=True)
    assert issue.fix == Fix("restore_night", label="settings.common.restore")
    assert collect(_snapshot([{"id": "a", "url": NNM + "1"}], data_lost=lost)) == []
    assert data_lost.folder_lost({"folder": 5}, []) is None


@pytest.mark.parametrize(
    ("last_scheduled", "listed"),
    [
        (NOW - 2 * 3600 - 10 * 60 - 1, True),  # two intervals and the slack have passed
        (NOW - 2 * 3600 - 10 * 60, False),
        (NOW - 60, False),
    ],
)
def test_scheduled_checks_are_late_after_two_intervals_and_the_slack(last_scheduled, listed):
    found = collect(_snapshot(next_check_at=last_scheduled + 3600))
    assert [issue.kind for issue in found] == (["check_stale"] if listed else [])
    if listed:
        assert found[0].severity == "warn"
        assert found[0].message == Message("issues.check_stale", {"at": last_scheduled})
        assert found[0].target == Target("settings", open="service", anchor="acc-service", note=True)
        assert found[0].fix == Fix("restart_service", label="settings.service.restart")
    assert collect(_snapshot(next_check_at=None)) == []


def test_the_watchdog_judges_lateness_by_the_same_rule():
    assert watchdog.checks_late is check_stale.late
    assert check_stale.late(0, 2 * 60 + check_stale.STALE_SLACK_SEC + 1, 60)
    assert not check_stale.late(0, 2 * 60 + check_stale.STALE_SLACK_SEC, 60)


def test_a_check_blocked_by_the_master_key_points_at_its_explanation():
    [issue] = collect(_snapshot(health={"check_ok": False, "check_error": "secrets_migration_required"}))
    assert (issue.kind, issue.severity, issue.message.key) == ("check_blocked", "bad", "issues.check_blocked_secrets")
    assert issue.target == Target("settings", anchor="secret-store")
    assert issue.fix == Fix("copy_keys_status", label="settings.page.copy_command")


def test_a_failed_check_points_at_its_error_in_the_history():
    [issue] = collect(_snapshot(health={"check_ok": False, "check_error": "error"}))
    assert (issue.kind, issue.severity, issue.message.key) == ("check_blocked", "bad", "issues.check_failed")
    assert issue.target == Target("history", filter=(("group", "errors"),), anchor="latest-check_blocked")
    assert issue.fix == Fix("check_all", label="base.check_all.title")
    # A failure a site caused is transport trouble: amber, as on Home's rows.
    assert collect(_snapshot(health={"check_ok": False, "check_error": "tracker"}))[0].severity == "warn"


def test_the_client_down_is_the_main_client():
    [issue] = collect(_snapshot(health={"qbit_ok": False}))
    assert (issue.kind, issue.severity, issue.subjects) == ("client_down", "bad", ("nas",))
    assert issue.target == Target("settings", open="clients", anchor="client-nas")
    assert issue.fix == Fix("check_client", "nas", label="settings.common.check")
    assert issue.fix.ref == "check_client:nas"
    assert collect(_snapshot(health={})) == []


def test_a_rebuilt_history_is_said_with_nothing_to_fix():
    [issue] = collect(_snapshot(health={"history_rebuilt_at": int(NOW)}))
    assert (issue.kind, issue.severity, issue.fix) == ("history_rebuilt", "warn", None)
    assert issue.target == Target("history")
    assert collect(_snapshot(health={"history_rebuilt_at": "yesterday"})) == []


@pytest.mark.parametrize(
    ("cls", "tone"),
    [("tracker", "warn"), ("frozen", "warn"), ("qbit", "bad"), ("quota", "bad"), ("disk", "bad"), ("error", "bad")],
)
def test_current_topic_failures_have_their_own_issue_and_landing(cls, tone):
    topic = {"id": "a", "title": "Show A", "last_error": "synthetic refusal", "last_error_class": cls}
    [issue] = collect(_snapshot([topic]))
    assert (issue.kind, issue.id, issue.severity) == ("topic_error", "topic_error:a", tone)
    assert issue.message.params["title"] == "Show A"
    assert issue.target == Target("home", open="a", anchor="row-a")
    assert issue.fix is None
    assert topic["last_error"] == "synthetic refusal"


@pytest.mark.parametrize(
    "topic",
    [
        {"id": "a"},
        {"id": "a", "last_error": ""},
        {"last_error": "timeout"},
        {"id": "a", "last_error": "timeout", "paused": True},
    ],
)
def test_topic_indicator_ignores_healthy_paused_or_unidentified_records(topic):
    assert collect(_snapshot([topic])) == []


def test_dead_default_client_is_not_counted_again_for_each_topic():
    topics = [
        {"id": "a", "last_error": "client down", "last_error_class": "qbit"},
        {"id": "b", "last_error": "client down", "last_error_class": "qbit", "client_id": "other"},
    ]
    found = collect(_snapshot(topics, {"qbit_ok": False}))
    assert [issue.id for issue in found] == ["client_down", "topic_error:b"]
    assert found[0].subjects == ("nas", "a")
    assert [issue.id for issue in collect(_snapshot(topics, {"qbit_ok": True}))] == ["topic_error:a", "topic_error:b"]


TWO_CLIENTS = {**CONFIG, "clients": {"nas": {"kind": "qbittorrent", "default": True}, "tr": {"kind": "transmission"}}}


def _two(topics=(), health=None) -> Snapshot:
    return Snapshot(state={"topics": list(topics), "health": health or {}}, config=TWO_CLIENTS, now=NOW)


def test_a_failed_check_in_settings_makes_the_client_down_as_the_header_says():
    """D5: Settings' "Check" that found the main client not answering turned the header red, while
    Needs attention said nothing: the issue reads the same mark (``ping_failed``) the header does."""
    [issue] = collect(_snapshot(health={"qbit_ok": True, "ping_failed": {"nas": 1}}))
    assert (issue.id, issue.severity, issue.subjects) == ("client_down", "bad", ("nas",))
    assert issue.message.key == "issues.client_down"
    # The check and "Check" both said it: one issue, not two.
    assert [i.id for i in collect(_snapshot(health={"qbit_ok": False, "ping_failed": {"nas": 1}}))] == ["client_down"]


@pytest.mark.parametrize("health", [{"ping_failed": {"tr": 1}}, {"clients_ok": {"nas": True, "tr": False}}])
def test_a_second_client_that_does_not_answer_is_its_own_issue(health):
    [issue] = collect(_two(health={"qbit_ok": True, **health}))
    assert (issue.kind, issue.id, issue.severity, issue.subjects) == ("client_down", "client_down:tr", "bad", ("tr",))
    assert issue.message == Message("issues.client_down_named", {"client": "Transmission (tr)"})
    assert issue.target == Target("settings", open="clients", anchor="client-tr")
    assert issue.fix == Fix("check_client", "tr", label="settings.common.check")


def test_each_client_down_is_said_once_the_main_one_first():
    found = collect(_two(health={"qbit_ok": False, "clients_ok": {"tr": False}, "ping_failed": {"tr": 1, "nas": 2}}))
    assert [issue.id for issue in found] == ["client_down", "client_down:tr"]
    # Two clients: the main one is named too, so the two entries are told apart.
    assert found[0].message == Message("issues.client_down_named", {"client": "qBittorrent (nas)"})


def test_a_client_no_longer_configured_is_not_down():
    assert collect(_two(health={"ping_failed": {"gone": 1}, "clients_ok": {"gone": False}})) == []


def test_the_topics_of_a_second_client_down_are_counted_in_its_issue():
    topics = [
        {"id": "a", "last_error": "client down", "last_error_class": "qbit"},
        {"id": "b", "last_error": "client down", "last_error_class": "qbit", "client_id": "tr"},
    ]
    found = collect(_two(topics, {"qbit_ok": True, "ping_failed": {"tr": 1}}))
    assert [issue.id for issue in found] == ["client_down:tr", "topic_error:a"]
    assert found[0].subjects == ("tr", "b")


def test_legacy_unknown_error_codes_use_the_saved_text():
    from tow.errors import render_stored

    topic = {"id": "a", "title": "Show A", "last_error": "legacy refusal", "last_error_code": "future.not_known"}
    [issue] = collect(_snapshot([topic]))
    assert render_stored(issue.message.key, dict(issue.message.params), "fallback") == "Show A — legacy refusal"


def test_typed_topic_error_uses_current_site_title_and_keeps_saved_evidence():
    import copy

    from tow.errors import TowError, render_stored

    error = TowError("mirrors.all_failed", tracker="nnmclub", error=TowError("mirrors.rate_limited"))
    record = error.record()
    topic = {
        "id": "a",
        "title": "Show A",
        "last_error": "old diagnostic",
        "last_error_class": "tracker",
        "last_error_code": record["code"],
        "last_error_params": record["params"],
    }
    before = copy.deepcopy(topic)
    [issue] = collect(_snapshot([topic]))
    rendered = render_stored(issue.message.key, dict(issue.message.params), "fallback")
    assert "NNM-Club" in rendered
    assert "old diagnostic" not in rendered
    assert "доступ к сайту ограничен" in rendered
    assert topic == before


@pytest.mark.parametrize("params", [["tracker", "nnmclub"], "nnmclub", 7])
def test_malformed_saved_error_params_still_list_the_topic(params):
    topic = {"id": "a", "title": "Show A", "last_error": "saved text", "last_error_class": "tracker"}
    topic["last_error_params"] = params
    [issue] = collect(_snapshot([topic]))
    assert issue.id == "topic_error:a"


@pytest.mark.parametrize("site", ["broken", ["nnmclub"], None])
def test_a_malformed_site_entry_does_not_break_the_site_access_issue(site):
    from tow.errors import TowError

    record = TowError("mirrors.all_failed", tracker="nnmclub", error=TowError("mirrors.rate_limited")).record()
    topic = {
        "id": "a",
        "title": "Show A",
        "last_error": "x",
        "last_error_class": "tracker",
        "last_error_code": record["code"],
        "last_error_params": record["params"],
    }
    config = {**CONFIG, "trackers": {**CONFIG["trackers"], "nnmclub": site}}
    [issue] = collect(Snapshot(state={"topics": [topic], "health": {}}, config=config, now=NOW))
    assert issue.subjects == ("a",)


def test_cleared_error_with_a_legacy_auth_class_does_not_hold_the_indicator():
    assert collect(_snapshot([{"id": "a", "url": NNM + "1", "last_error_class": "tracker_auth"}])) == []


def test_auth_error_without_a_saved_class_still_has_a_problem_link():
    [issue] = collect(_snapshot([{"id": "a", "url": NNM + "1", "last_error": "tracker auth required"}]))
    assert issue.kind == "topic_error"
    assert issue.severity == "bad"


def test_sign_ins_are_one_issue_per_site_counted_over_topics_not_paused():
    topics = [
        _auth("r1", RUTOR + "1"),
        _auth("a", NNM + "1"),
        {"id": "x", "url": NNM + "9", "last_error_class": "qbit"},
        _auth("b", NNM + "2"),
        _auth("c", NNM + "3", paused=True),
        _auth("u", "https://unknown.example/t/4"),
    ]
    found = collect(_snapshot(topics))
    assert [(issue.id, issue.count, issue.subjects) for issue in found] == [
        ("site_needs_login:nnmclub", 2, ("a", "b")),
        ("site_needs_login:rutor", 1, ("r1",)),
        ("site_needs_login:", 1, ("u",)),  # an unknown site last
    ]
    nnm, rutor, unknown = found
    assert nnm.message == Message("issues.site_needs_login", {"site": "NNM-Club", "count": 2})
    # Several topics: the site's topics with a problem, the first one open, its sign-in focused.
    assert nnm.target == Target("home", open="a", filter=(("f", "problem"), ("t", "nnmclub")), anchor="row-a")
    assert nnm.fix == Fix("sign_in", "a", label="home.topic.sign_in")
    # A site without a sign-in page has no sign-in to offer: it asks for one, and its row on
    # Sites opens at the user name, where a sign-in is added.
    assert rutor.message == Message("issues.site_login_missing", {"site": "rutor", "count": 1})
    assert rutor.target == Target("sites", open="rutor", anchor="site-row-rutor", note=True)
    assert rutor.target_many is None
    assert rutor.fix == Fix("site_login", "rutor", label="sites.login")
    assert unknown.message == Message("issues.site_needs_login_unknown", {"count": 1})
    assert unknown.target == Target("home", open="u", anchor="row-u")
    assert unknown.fix is None  # a site TOW does not know: the open row shows the error
    assert {issue.severity for issue in found} == {"bad"}


def test_sign_ins_read_the_sites_through_the_trackers_module_on_each_call(monkeypatch):
    """site_needs_login copied load_trackers and match_tracker at import. The kinds are loaded
    when Needs attention is first listed: a test that replaced them then would have left its
    replacement there for the rest of the run (topic_error's failure in 1.31.2)."""
    from tow import trackers

    issues.known_kinds()  # loaded before the replacement, as in a full run
    asked = []
    monkeypatch.setattr(trackers, "load_trackers", lambda config: asked.append("sites") or {})
    monkeypatch.setattr(trackers, "match_tracker", lambda found, url: asked.append(url) and None)
    [issue] = collect(_snapshot([_auth("a", NNM + "1")]))
    assert issue.id == "site_needs_login:"  # no site matched: the replacements answered
    assert asked == ["sites", NNM + "1"]


def test_after_a_sign_in_the_topics_refused_before_it_wait_for_their_next_check():
    before = "2027-01-15T08:00:00+00:00"  # NOW is 2027-01-15T08:00:00Z
    after = "2027-01-15T08:05:00+00:00"
    signed = NOW + 60  # the sign-in, confirmed a minute later
    state = {
        "topics": [
            _auth("a", NNM + "1", last_check=before),
            _auth("b", NNM + "2", last_check=after),  # refused again after the sign-in: asks again
            _auth("c", NNM + "3", last_check="15.01.2027 08:00"),  # a check time of TOW 1.24
            _auth("r", RUTOR + "1", last_check=before),  # no sign-in page: it never "signed in"
        ],
        "mirrors": {"nnmclub": {"signed_in_at": signed}, "rutor": {"signed_in_at": signed}},
    }
    found = collect(Snapshot(state=state, config=CONFIG, now=NOW))
    assert [(issue.id, issue.severity, issue.subjects) for issue in found] == [
        ("site_needs_login:nnmclub", "bad", ("b",)),
        ("site_needs_login:rutor", "bad", ("r",)),
        ("site_needs_login:nnmclub:signed_in", "warn", ("a", "c")),
    ]
    waiting = found[2]
    assert waiting.message == Message("issues.site_signed_in", {"site": "NNM-Club", "count": 2})
    # Nothing is checked by itself (a check may download a .torrent): the owner's "Check all".
    assert waiting.fix == Fix("check_all", label="base.check_all.title")
    assert waiting.target == Target("home", open="a", filter=(("f", "problem"), ("t", "nnmclub")), anchor="row-a")
    # The topics keep the error their last check saved.
    assert all(topic["last_error_class"] == "tracker_auth" for topic in state["topics"])


@pytest.mark.parametrize("signed_in_at", [None, "yesterday", True, float("nan")])
def test_no_sign_in_on_record_asks_to_sign_in(signed_in_at):
    state = {
        "topics": [_auth("a", NNM + "1", last_check="2027-01-15T08:00:00+00:00")],
        "mirrors": {"nnmclub": {"signed_in_at": signed_in_at}},
    }
    [issue] = collect(Snapshot(state=state, config=CONFIG, now=NOW))
    assert issue.id == "site_needs_login:nnmclub"


def test_worst_first_then_the_kinds_order():
    everything = _snapshot(
        [_auth("a", NNM + "1")],
        {"qbit_ok": False, "check_ok": False, "history_rebuilt_at": 1},
        next_check_at=NOW - 3 * 3600,
    )
    assert _kinds(everything) == ["check_blocked", "client_down", "site_needs_login", "check_stale", "history_rebuilt"]
    assert issues.worst(collect(everything)) == "bad"
    assert issues.worst(collect(_snapshot(health={"history_rebuilt_at": 1}))) == "warn"


def test_the_fingerprint_follows_the_set_of_problems_not_its_order():
    one = collect(_snapshot([_auth("a", NNM + "1")], {"qbit_ok": False}))
    assert fingerprint(one) == fingerprint(list(reversed(one)))
    assert len(fingerprint(one)) == 16
    more = collect(_snapshot([_auth("a", NNM + "1"), _auth("b", NNM + "2")], {"qbit_ok": False}))
    assert fingerprint(more) != fingerprint(one)  # another topic asks for a sign-in
    fewer = collect(_snapshot([_auth("a", NNM + "1")], {}))
    assert fingerprint(fewer) != fingerprint(one)  # the client answers again
    assert fingerprint(collect(_snapshot([_auth("a", NNM + "1")], {"qbit_ok": False}))) == fingerprint(one)


def test_a_failing_rule_is_left_out_and_logged(caplog):
    def broken(_snapshot):
        raise KeyError("health")

    def fine(_snapshot):
        return [Issue("fine", "fine", "warn", Message("issues.check_failed"), Target("history"))]

    kinds = (collect_module.Kind("broken", 1, broken), collect_module.Kind("fine", 2, fine))
    with caplog.at_level(logging.WARNING, logger="tow.issues"):
        assert [issue.kind for issue in collect(_snapshot(), only=kinds)] == ["fine"]
    assert "issue kind broken failed: KeyError" in caplog.text


def test_a_broken_kind_module_is_skipped():
    with pytest.raises(TypeError, match="no KIND"):
        collect_module._kind(types.ModuleType("tow.issues.kinds.empty"))
    module = types.ModuleType("tow.issues.kinds.half")
    module.KIND, module.ORDER = "half", True
    with pytest.raises(TypeError, match="ORDER"):
        collect_module._kind(module)
    module.ORDER = 3
    with pytest.raises(TypeError, match="collect"):
        collect_module._kind(module)


def test_an_unknown_severity_is_refused():
    with pytest.raises(ValueError, match="severity"):
        Issue("x", "x", "red", Message("k"), Target("home"))


def test_a_client_turned_off_is_not_down():
    # A client turned off in Settings is never asked again: its last "no answer" stayed for ever.
    off = {**TWO_CLIENTS, "clients": {**TWO_CLIENTS["clients"], "tr": {"kind": "transmission", "enabled": False}}}
    snapshot = Snapshot(
        state={"topics": [], "health": {"qbit_ok": True, "clients_ok": {"tr": False}, "ping_failed": {"tr": 1}}},
        config=off,
        now=NOW,
    )
    assert collect(snapshot) == []
    # With only one client left on, the main one's issue does not name it among several.
    snapshot = Snapshot(state={"topics": [], "health": {"qbit_ok": False}}, config=off, now=NOW)
    assert [issue.message for issue in collect(snapshot)] == [Message("issues.client_down")]


def test_a_check_keeps_no_word_of_a_client_no_longer_configured():
    from tow.check.client_ops import ClientPool
    from tow.notify import NotificationBatch

    pool = ClientPool(
        cfg=TWO_CLIENTS,
        apply=True,
        notify=False,
        how="auto",
        record=lambda *_a, **_k: None,
        batch=NotificationBatch(),
        default_id="nas",
        previous_qbit_ok=True,
        previous_clients_ok={"tr": True, "gone": False},
    )
    assert pool.health() == {"tr": True, "nas": True}
