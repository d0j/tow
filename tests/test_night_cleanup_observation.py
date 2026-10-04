"""Night cleanup is a scoped observation; lost evidence is not completion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tow import snapshots, watchdog
from tow.config import load_config, save_config
from tow.i18n import t
from tow.pulse import Probes
from tow.store import save_secrets, save_state
from tow.web import app

NOW = 1791072000


@pytest.fixture
def night(tmp_path_factory):
    folder = tmp_path_factory.mktemp("night-copies") / "night"
    cfg = load_config()
    cfg.update(language="en", backup_dir=str(folder))
    cfg.pop("heartbeat_url", None)
    save_config(cfg)
    save_secrets({})
    save_state({"topics": [], "mirrors": {}, "health": {"auto_at_ts": NOW}})
    return folder


def _check(sent):
    return watchdog.run_watchdog(
        is_healthy=lambda _port: True,
        deploy_running=lambda: False,
        now=lambda: NOW,
        sleep=lambda _seconds: None,
        send=lambda text: sent.append(text) or True,
        flush=lambda: None,
        probes=Probes.inert(),
    )


def _record(night, pending):
    snapshots._record(
        last_ok_at=NOW,
        last_error="",
        location=str(night.resolve()),
        last_cleanup_pending=pending,
        cleanup_inventory=snapshots._cleanup_inventory(night),
    )


@pytest.mark.parametrize("mode", ["missing", "empty", "partial", "null", "malformed", "failure-after-loss"])
@pytest.mark.parametrize("pending", [True, False])
def test_lost_record_preserves_last_confirmed_cleanup_and_does_not_resolve(night, pending, mode):
    # Seed the old format so that this reproduces the pre-inventory implementation too.
    snapshots._record(last_ok_at=NOW, last_error="", location=str(night.resolve()), last_cleanup_pending=pending)
    sent = []
    _check(sent)
    # Unbound success must remain unknown; a legacy warning remains useful.
    if not pending:
        _record(night, pending)
        _check(sent)
    sent.clear()
    path = snapshots.status_path()
    if mode in ("missing", "failure-after-loss"):
        path.unlink()
    else:
        path.write_text(
            {
                "empty": "{}",
                "partial": json.dumps({"last_ok_at": NOW}),
                "null": '{"last_cleanup_pending":null}',
                "malformed": "{",
            }[mode],
            encoding="utf-8",
        )
    if mode == "failure-after-loss":
        snapshots.record_failure("synthetic copy refusal")
    raw = path.read_bytes() if path.exists() else None
    for _ in range(3):
        report = _check(sent)
        assert report["backup_cleanup_pending"] is None
        assert report["backup_cleanup_read_error"] is True
        assert watchdog._load_state()["backup_cleanup"] is not pending
        assert not any(t("watchdog.alert.backup_cleanup_ok", "en") in message for message in sent)
        assert (path.read_bytes() if path.exists() else None) == raw


def test_changed_night_folder_cannot_resolve_an_old_warning(night):
    _record(night, True)
    sent = []
    _check(sent)
    cfg = load_config()
    cfg["backup_dir"] = str(night.parent / "other-night")
    save_config(cfg)
    _check(sent)
    _record(night.parent / "other-night", False)
    _check(sent)
    assert sent == [t("watchdog.alert.backup_cleanup_pending", "en")]


@pytest.mark.parametrize("pending", [True, False])
def test_inventory_change_invalidates_record_without_mutating_copies(night, pending):
    _record(night, pending)
    sent = []
    _check(sent)
    night.mkdir()
    copy = night / "tow-20261004-000000"
    copy.mkdir()
    marker = copy / "MANIFEST.json"
    marker.write_text("synthetic untrusted copy", encoding="utf-8")
    raw = snapshots.status_path().read_bytes()
    report = _check(sent)
    assert report["backup_cleanup_pending"] is None
    assert report["backup_cleanup_read_error"] is True
    assert watchdog._load_state()["backup_cleanup"] is not pending
    assert snapshots.status_path().read_bytes() == raw
    assert marker.read_text(encoding="utf-8") == "synthetic untrusted copy"


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_unknown_cleanup_is_visible_without_hiding_existing_copy(night, lang):
    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)
    made = snapshots.create_snapshot()
    snapshots.status_path().unlink()
    response = TestClient(app, headers={"Accept-Language": lang}).get("/settings")
    assert response.status_code == 200
    card = response.text.split('id="backup-night"', 1)[1].split("</article>", 1)[0]
    assert 'class="pill warn"' in card
    assert t("settings.backups.pill_unknown", lang) in card
    assert Path(made["snapshot"]).name in card


@pytest.mark.parametrize("pending", [True, False])
def test_legacy_migration_is_quiet_and_never_resolves_cleanup(night, pending):
    snapshots._record(last_ok_at=NOW, last_error="", location=str(night.resolve()), last_cleanup_pending=pending)
    watchdog._save_state({"backup_cleanup": not pending})
    sent = []
    for _ in range(3):
        report = _check(sent)
        assert report["backup_cleanup_pending"] is (True if pending else None)
        assert report["backup_cleanup_read_error"] is False
    assert not sent


def test_new_copy_binds_its_cleanup_result_to_current_inventory(night):
    snapshots.create_snapshot()
    observed = snapshots.cleanup_status()
    assert observed == {"pending": False, "read_error": False, "legacy": False, "location": str(night.resolve())}
    assert snapshots.status()["cleanup_inventory"] == snapshots._cleanup_inventory(night)


def test_fresh_install_missing_record_is_unknown_not_a_failure(night):
    sent = []
    report = _check(sent)
    assert report["backup_cleanup_pending"] is None
    assert report["backup_cleanup_read_error"] is False
    assert not sent


@pytest.mark.parametrize("pending", [True, False])
def test_readability_recovery_is_not_cleanup_completion(night, pending):
    _record(night, pending)
    sent = []
    _check(sent)
    sent.clear()
    snapshots.status_path().unlink()
    for _ in range(3):
        _check(sent)
    assert sent == [t("watchdog.alert.backup_cleanup_unreadable", "en")]
    _record(night, pending)
    _check(sent)
    assert sent == [
        t("watchdog.alert.backup_cleanup_unreadable", "en"),
        t("watchdog.alert.backup_cleanup_readable", "en"),
    ]
    _check(sent)
    assert len(sent) == 2
    if pending:
        _record(night, False)
        _check(sent)
        assert sent[-1] == t("watchdog.alert.backup_cleanup_ok", "en")


@pytest.mark.parametrize("pending", [True, False])
def test_bound_record_losing_inventory_is_not_legacy_migration(night, pending):
    _record(night, pending)
    sent = []
    _check(sent)
    sent.clear()
    state = snapshots.status()
    del state["cleanup_inventory"]
    snapshots.status_path().write_text(json.dumps(state), encoding="utf-8")
    for _ in range(3):
        report = _check(sent)
        assert report["backup_cleanup_pending"] is None
        assert report["backup_cleanup_read_error"] is True
        assert watchdog._load_state()["backup_cleanup"] is not pending
    assert sent == [t("watchdog.alert.backup_cleanup_unreadable", "en")]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cleanup_inventory", None),
        ("cleanup_inventory", "a" * 63),
        ("cleanup_inventory", 1),
        ("location", ""),
        ("last_cleanup_pending", None),
        ("last_cleanup_pending", 1),
    ],
)
def test_invalid_cleanup_schema_is_read_only_unknown(night, field, value):
    _record(night, False)
    state = snapshots.status()
    state[field] = value
    path = snapshots.status_path()
    path.write_text(json.dumps(state), encoding="utf-8")
    raw = path.read_bytes()
    result = snapshots.cleanup_status()
    assert result["pending"] is None
    assert result["read_error"] is True
    assert path.read_bytes() == raw


@pytest.mark.parametrize("pending", [True, False])
def test_failed_monitoring_write_cannot_confirm_stale_result(night, monkeypatch, pending):
    from tow import store

    snapshots.create_snapshot()
    _record(night, pending)
    sent = []
    _check(sent)
    raw = snapshots.status_path().read_bytes()
    original = store.atomic_write_text

    def denied(path, *args, **kwargs):
        if path == snapshots.status_path():
            raise PermissionError("synthetic monitoring refusal")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(store, "atomic_write_text", denied)
    made = snapshots.create_snapshot()
    assert made["ok"] is True
    assert snapshots.verify_snapshot(Path(made["snapshot"]))["signed"] is True
    assert snapshots.status_path().read_bytes() == raw
    report = _check(sent)
    assert report["backup_cleanup_pending"] is None
    assert report["backup_cleanup_read_error"] is True
    assert watchdog._load_state()["backup_cleanup"] is not pending


def test_inventory_failure_does_not_fail_a_verified_copy_or_reuse_old_binding(night, monkeypatch):
    snapshots.create_snapshot()

    def denied(_folder):
        raise PermissionError("synthetic inventory refusal")

    monkeypatch.setattr(snapshots, "_cleanup_inventory", denied)
    made = snapshots.create_snapshot()
    assert made["ok"] is True
    assert snapshots.verify_snapshot(Path(made["snapshot"]))["signed"] is True
    assert snapshots.status()["cleanup_inventory"] is None
    assert snapshots.cleanup_status()["read_error"] is True


def test_snapshot_record_is_written_before_creation_lock_is_released(night, monkeypatch):
    import contextlib

    active = []
    original_lock = snapshots.persistence_lock
    original_record = snapshots._record

    @contextlib.contextmanager
    def lock():
        with original_lock():
            active.append(True)
            try:
                yield
            finally:
                active.pop()

    def record(**fields):
        assert active
        original_record(**fields)

    monkeypatch.setattr(snapshots, "persistence_lock", lock)
    monkeypatch.setattr(snapshots, "_record", record)
    snapshots.create_snapshot()


def test_nonregular_record_is_refused_before_open(night, monkeypatch):
    path = snapshots.status_path()
    path.mkdir()

    def refuse_open(*_args, **_kwargs):
        pytest.fail("nonregular diagnostic record was opened")

    monkeypatch.setattr(snapshots, "read_object", refuse_open)
    assert snapshots.status()["read_error"] is True
    assert snapshots.cleanup_status()["read_error"] is True


def test_partials_and_unrelated_names_do_not_change_committed_inventory(night):
    _record(night, False)
    night.mkdir()
    (night / ".tow-20261004-000000.partial").mkdir()
    (night / "foreign").mkdir()
    assert snapshots.cleanup_status()["pending"] is False


@pytest.mark.parametrize("mode", ["permission", "not-directory"])
def test_legacy_record_does_not_hide_real_folder_access_failure(night, monkeypatch, mode):
    snapshots._record(last_ok_at=NOW, last_error="", location=str(night.resolve()), last_cleanup_pending=False)
    if mode == "not-directory":
        night.write_bytes(b"not a folder")
    else:
        night.mkdir()
        original = Path.iterdir

        def iterdir(path):
            if path == night:
                raise PermissionError("synthetic directory refusal")
            return original(path)

        monkeypatch.setattr(Path, "iterdir", iterdir)
    assert snapshots.cleanup_status()["read_error"] is True
