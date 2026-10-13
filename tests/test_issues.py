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
        "history_rebuilt",
        "site_needs_login",
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
    # One topic: its own row. A site without a sign-in page offers no sign-in.
    assert rutor.message == Message("issues.site_needs_login", {"site": "rutor", "count": 1})
    assert rutor.target == Target("home", open="r1", anchor="row-r1")
    assert rutor.fix is None
    assert unknown.message == Message("issues.site_needs_login_unknown", {"count": 1})
    assert {issue.severity for issue in found} == {"bad"}


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
