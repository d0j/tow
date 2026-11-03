"""A monitor transition survives both a locked secret store and a crash during its commit."""

from __future__ import annotations

import httpx
import pytest

from tow import delivery, watchdog
from tow.config import load_config, save_config
from tow.notifiers import base
from tow.pulse import Machine, Probes
from tow.store import SecretStoreError, load_state, save_secrets, save_state

NOW = 1_793_664_000.0
DISCORD = {"notifiers": {"discord": {"webhook_url": "https://discord.com/api/webhooks/1/synthetic"}}}


@pytest.fixture
def monitor(monkeypatch):
    cfg = load_config()
    cfg.update(backup_enabled=False, quiet_hours=None, interval_sec=3600)
    save_config(cfg)
    save_state({"topics": [], "health": {"auto_at_ts": NOW}})
    save_secrets(DISCORD)
    sent = []
    monkeypatch.setattr(
        base,
        "http_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(204))
        ),
    )
    return sent


def run(*, up=False, **kwargs):
    return watchdog.run_watchdog(
        is_healthy=lambda _port: up,
        deploy_running=lambda: False,
        now=lambda: NOW,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_locked_secrets_keep_the_transition_until_a_later_unchanged_pass(monitor, monkeypatch):
    from tow import store

    original = store.load_secrets
    monkeypatch.setattr(store, "load_secrets", lambda: (_ for _ in ()).throw(SecretStoreError("synthetic lock")))
    assert run()["delivered"] is False
    assert len(load_state()["notify_pending"]) == 1
    assert not monitor
    monkeypatch.setattr(store, "load_secrets", original)
    assert run()["alerts"] == []
    assert len(monitor) == 1
    assert not load_state().get("notify_pending")
    assert not load_state().get("notify_outbox")


@pytest.mark.parametrize("delivered_before_retry", [False, True])
def test_crash_after_staging_cannot_lose_or_repeat_an_already_delivered_transition(
    monitor,
    monkeypatch,
    delivered_before_retry,
):
    original = watchdog._save_state
    monkeypatch.setattr(watchdog, "_save_state", lambda _state: (_ for _ in ()).throw(OSError("synthetic crash")))
    with pytest.raises(OSError, match="synthetic crash"):
        run()
    token = load_state()["watchdog_notice_revision"]
    if delivered_before_retry:
        watchdog._flush_queued_messages()
        assert len(monitor) == 1
    monkeypatch.setattr(watchdog, "_save_state", original)
    run()
    run()
    assert load_state()["watchdog_notice_revision"] == token
    assert len(monitor) == 1
    assert not load_state().get("notify_pending")
    assert not load_state().get("notify_outbox")


def test_a_concurrent_newer_observation_is_not_rewound_or_notified(monitor):
    probes = Probes.inert()
    probes.machine = lambda: run(up=True) and None
    result = run(probes=probes)
    assert result["superseded"] is True
    assert result["alerts"] == []
    assert watchdog._load_state()["service"] is True
    assert not monitor


def test_quiet_transition_is_staged_once_even_before_diagnostic_write(monitor, monkeypatch):
    monkeypatch.setattr(delivery, "quiet_now", lambda *_args, **_kwargs: True)
    original = watchdog._save_state
    monkeypatch.setattr(watchdog, "_save_state", lambda _state: (_ for _ in ()).throw(OSError("synthetic crash")))
    with pytest.raises(OSError, match="synthetic crash"):
        run()
    monkeypatch.setattr(watchdog, "_save_state", original)
    assert run()["held"] is True
    assert len(load_state()["notify_queue"]) == 1
    assert not monitor


@pytest.mark.parametrize("source", ["boot", "logon", "wake", "folder"])
def test_a_new_logical_source_is_not_suppressed_when_the_diagnostic_write_keeps_failing(
    monitor,
    monkeypatch,
    tmp_path,
    source,
):
    facts = {"boot": NOW - 100, "logon": NOW - 90}
    probes = Probes.inert()
    probes.machine = lambda: Machine(boot_ts=facts["boot"], asleep_sec=0, logon_ts=facts["logon"])
    monkeypatch.setattr(watchdog, "_save_state", lambda _state: (_ for _ in ()).throw(OSError("synthetic crash")))
    with pytest.raises(OSError, match="synthetic crash"):
        run(probes=probes, wake_ts=NOW - 80)
    original = load_state()["watchdog_notice_revision"]
    watchdog._flush_queued_messages()
    assert len(monitor) == 1
    if source in {"boot", "logon"}:
        facts[source] += 20
    elif source == "folder":
        cfg = load_config()
        cfg["restore_points_dir"] = str(tmp_path.parent / (tmp_path.name + "-new-folder"))
        save_config(cfg)
    wake = NOW - 60 if source == "wake" else NOW - 80
    with pytest.raises(OSError, match="synthetic crash"):
        run(probes=probes, wake_ts=wake)
    assert load_state()["watchdog_notice_revision"] != original
    watchdog._flush_queued_messages()
    assert len(monitor) == 2
    with pytest.raises(OSError, match="synthetic crash"):
        run(probes=probes, wake_ts=wake)
    watchdog._flush_queued_messages()
    assert len(monitor) == 2


def test_elapsed_time_alone_does_not_restage_the_same_uncommitted_notice(monitor, monkeypatch):
    monkeypatch.setattr(watchdog, "_save_state", lambda _state: (_ for _ in ()).throw(OSError("synthetic crash")))
    with pytest.raises(OSError, match="synthetic crash"):
        run()
    watchdog._flush_queued_messages()
    with pytest.raises(OSError, match="synthetic crash"):
        watchdog.run_watchdog(
            is_healthy=lambda _port: False,
            deploy_running=lambda: False,
            now=lambda: NOW + 600,
            sleep=lambda _seconds: None,
        )
    watchdog._flush_queued_messages()
    assert len(monitor) == 1


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("ru", "Ожидается первая ночная копия."),
        ("en", "Waiting for the first nightly backup."),
    ],
)
def test_cli_first_backup_pending_is_never_reported_as_disabled(monkeypatch, lang, expected):
    from tow import i18n
    from tow.cli.render import watchdog_text

    monkeypatch.setattr(i18n, "current", lambda: lang)
    report = {
        "service_ok": True,
        "checks_ok": True,
        "port": 8787,
        "backup_ok": None,
        "backup_state": "pending",
        "alerts": [],
    }
    text = watchdog_text(report)
    assert expected in text
    assert i18n.t("cli.watchdog.backup_off") not in text
    report["backup_state"] = "disabled"
    assert i18n.t("cli.watchdog.backup_off") in watchdog_text(report)
    assert expected not in watchdog_text(report)
