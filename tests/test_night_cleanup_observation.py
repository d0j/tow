"""Night cleanup is a scoped observation; lost evidence is not completion."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tow import snapshots, watchdog
from tow.config import load_config, save_config
from tow.i18n import t
from tow.pulse import Probes
from tow.snapshots import create as night_create
from tow.snapshots import record as night_record
from tow.store import persistence_lock, save_secrets, save_state
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
    night_record.record_status(
        last_ok_at=NOW,
        last_error="",
        location=str(night.resolve()),
        last_cleanup_pending=pending,
        **night_record.cleanup_inventory(night),
    )


@pytest.mark.parametrize("mode", ["missing", "empty", "partial", "null", "malformed", "failure-after-loss"])
@pytest.mark.parametrize("pending", [True, False])
def test_lost_record_preserves_last_confirmed_cleanup_and_does_not_resolve(night, pending, mode):
    # Seed the old format so that this reproduces the pre-inventory implementation too.
    night_record.record_status(
        last_ok_at=NOW, last_error="", location=str(night.resolve()), last_cleanup_pending=pending
    )
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
    raw = snapshots.status_path().read_bytes()
    with persistence_lock():  # a copy committed without its monitoring write
        copy = Path(night_create._create_snapshot(how="auto")["snapshot"])
    manifest = (copy / "MANIFEST.json").read_bytes()
    report = _check(sent)
    assert report["backup_cleanup_pending"] is None
    assert report["backup_cleanup_read_error"] is True
    assert watchdog._load_state()["backup_cleanup"] is not pending
    assert snapshots.status_path().read_bytes() == raw
    assert (copy / "MANIFEST.json").read_bytes() == manifest


@pytest.mark.parametrize("pending", [True, False])
def test_a_copy_deleted_by_hand_or_a_foreign_folder_keeps_the_observation(night, pending):
    first = Path(snapshots.create_snapshot()["snapshot"])
    snapshots.create_snapshot()
    _record(night, pending)
    sent = []
    _check(sent)
    shutil.rmtree(first)  # in the file manager, not through TOW
    foreign = night / "tow-copied-by-hand"
    foreign.mkdir()
    (foreign / "MANIFEST.json").write_text("synthetic untrusted copy", encoding="utf-8")

    report = _check(sent)

    assert report["backup_cleanup_pending"] is pending
    assert report["backup_cleanup_read_error"] is False
    assert (foreign / "MANIFEST.json").read_text(encoding="utf-8") == "synthetic untrusted copy"


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
    night_record.record_status(
        last_ok_at=NOW, last_error="", location=str(night.resolve()), last_cleanup_pending=pending
    )
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
    assert snapshots.status()["cleanup_inventory"] == night_record.cleanup_inventory(night)["cleanup_inventory"]


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

    monkeypatch.setattr(night_create, "cleanup_inventory", denied)
    made = snapshots.create_snapshot()
    assert made["ok"] is True
    assert snapshots.verify_snapshot(Path(made["snapshot"]))["signed"] is True
    assert snapshots.status()["cleanup_inventory"] is None
    assert snapshots.cleanup_status()["read_error"] is True


def test_snapshot_record_is_written_before_creation_lock_is_released(night, monkeypatch):
    import contextlib

    active = []
    original_lock = night_create.persistence_lock
    original_record = night_record.record_status

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

    monkeypatch.setattr(night_create, "persistence_lock", lock)
    monkeypatch.setattr(night_create, "record_status", record)
    snapshots.create_snapshot()


def test_nonregular_record_is_refused_before_open(night, monkeypatch):
    path = snapshots.status_path()
    path.mkdir()

    def refuse_open(*_args, **_kwargs):
        pytest.fail("nonregular diagnostic record was opened")

    monkeypatch.setattr(night_record, "read_object", refuse_open)
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
    night_record.record_status(last_ok_at=NOW, last_error="", location=str(night.resolve()), last_cleanup_pending=False)
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


def _pre_cleanup_record(night):
    return {
        "last_ok_at": NOW,
        "last_snapshot": "tow-20261001-000000",
        "last_bytes": 1,
        "last_missing": [],
        "last_error": "",
        "location": str(night.resolve()),
    }


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("prior_alarm", [True, False])
def test_pre_cleanup_copy_metadata_migrates_quietly_without_confirming_completion(night, lang, prior_alarm):
    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)
    night_record.record_status(**_pre_cleanup_record(night))
    # Older watchdogs recorded True when the cleanup field did not exist.
    watchdog._save_state({"backup_cleanup": True, "backup_cleanup_monitoring": not prior_alarm})
    sent = []
    for _ in range(3):
        report = _check(sent)
        assert report["backup_cleanup_pending"] is None
        assert report["backup_cleanup_read_error"] is False
        assert report["backup_cleanup_legacy_migration"] is True
        assert watchdog._load_state()["backup_cleanup_monitoring"] is True
    assert not sent


@pytest.mark.parametrize("mode", ["missing-snapshot", "unsafe-snapshot", "missing-location", "bound-missing-flag"])
def test_incomplete_or_bound_metadata_is_not_pre_cleanup_migration(night, tmp_path, mode):
    record = _pre_cleanup_record(night)
    if mode == "missing-snapshot":
        del record["last_snapshot"]
    elif mode == "unsafe-snapshot":
        record["last_snapshot"] = "../tow-20261001-000000"
    elif mode == "missing-location":
        del record["location"]
    else:
        record.update(night_record.cleanup_inventory(night))
    (tmp_path / snapshots.STATUS_NAME).write_text(json.dumps(record), encoding="utf-8")
    watchdog._save_state({"backup_cleanup": False})
    sent = []
    report = _check(sent)
    assert report["backup_cleanup_pending"] is None
    assert report["backup_cleanup_read_error"] is True
    assert report["backup_cleanup_legacy_migration"] is False
    assert watchdog._load_state()["backup_cleanup"] is False
    assert sent == [t("watchdog.alert.backup_cleanup_unreadable", "en")]


def test_losing_both_bound_fields_cannot_masquerade_as_a_legacy_migration(night, tmp_path):
    _record(night, False)
    sent = []
    _check(sent)
    (tmp_path / snapshots.STATUS_NAME).write_text(json.dumps(_pre_cleanup_record(night)), encoding="utf-8")
    report = _check(sent)
    assert report["backup_cleanup_pending"] is None
    assert report["backup_cleanup_read_error"] is True
    assert report["backup_cleanup_legacy_migration"] is False
    assert sent == [t("watchdog.alert.backup_cleanup_unreadable", "en")]


@pytest.mark.parametrize("mode", ["permission", "not-directory"])
def test_pre_cleanup_metadata_cannot_hide_a_folder_access_error(night, monkeypatch, mode):
    night_record.record_status(**_pre_cleanup_record(night))
    if mode == "not-directory":
        night.write_bytes(b"synthetic non-directory")
    else:
        night.mkdir()
        original = Path.iterdir

        def denied(path):
            if path == night:
                raise PermissionError("synthetic directory refusal")
            return original(path)

        monkeypatch.setattr(Path, "iterdir", denied)
    result = snapshots.cleanup_status()
    assert result["read_error"] is True
    assert result["legacy"] is False


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_settings_explain_pre_cleanup_metadata_without_claiming_inaccessible_folder(night, tmp_path, lang):
    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)
    snapshots.create_snapshot()
    record = snapshots.status()
    del record["last_cleanup_pending"]
    del record["cleanup_inventory"]
    (tmp_path / snapshots.STATUS_NAME).write_text(json.dumps(record), encoding="utf-8")
    response = TestClient(app, headers={"Accept-Language": lang}).get("/settings")
    assert response.status_code == 200
    card = response.text.split('id="backup-night"', 1)[1].split("</article>", 1)[0]
    assert t("backup.snapshot.cleanup_legacy", lang) in card
    assert t("backup.snapshot.cleanup_unknown", lang) not in card
    assert t("settings.backups.pill_unknown", lang) in card


# --- a moved install (r1321 X3) ------------------------------------------------------------------


@pytest.fixture
def default_night(tmp_path, monkeypatch):
    """The night copies in their default folder, backup/night inside the install (data/ beside it)."""
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    cfg = load_config()
    cfg.update(language="en")
    cfg.pop("backup_dir", None)
    cfg.pop("heartbeat_url", None)
    save_config(cfg)
    save_secrets({})
    save_state({"topics": [], "mirrors": {}, "health": {"auto_at_ts": NOW}})
    return tmp_path / "backup" / "night"


def _move_install(old: Path, tmp_path_factory, monkeypatch, *, keep_old: bool = False) -> Path:
    """The documented move: the whole folder elsewhere, TOW started from there."""
    new = tmp_path_factory.mktemp("moved") / "TOW"
    shutil.copytree(old, new, symlinks=True)
    if not keep_old:
        shutil.rmtree(old / "backup")
    monkeypatch.setenv("TOW_ROOT", str(new))
    monkeypatch.setenv("TOW_HOME", str(new / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(new / "config.yaml"))
    return new


@pytest.mark.parametrize("keep_old", [False, True])
def test_a_moved_or_copied_install_keeps_its_night_cleanup_result(
    default_night, tmp_path, tmp_path_factory, monkeypatch, keep_old
):
    snapshots.create_snapshot()
    assert snapshots.status()["location_in_root"] == "backup/night"
    new = _move_install(tmp_path, tmp_path_factory, monkeypatch, keep_old=keep_old)
    observed = snapshots.cleanup_status()
    assert observed == {
        "pending": False,
        "read_error": False,
        "legacy": False,
        "location": str((new / "backup" / "night").resolve()),
    }


def test_a_record_from_before_the_move_binding_stays_unknown_after_a_move(
    default_night, tmp_path, tmp_path_factory, monkeypatch
):
    # Written by 1.32.0 and older: an absolute location only. Nothing proves the folder it names
    # is this one, so it stays unknown until the next night copy - as before.
    snapshots.create_snapshot()
    record = json.loads(snapshots.status_path().read_text(encoding="utf-8"))
    record.pop("location_in_root")
    snapshots.status_path().write_text(json.dumps(record), encoding="utf-8")
    _move_install(tmp_path, tmp_path_factory, monkeypatch)
    observed = snapshots.cleanup_status()
    assert observed["pending"] is None
    assert observed["read_error"] is False
    snapshots.create_snapshot()  # the next copy binds again
    assert snapshots.cleanup_status()["pending"] is False


def test_the_install_binding_never_reaches_another_folder(default_night, tmp_path):
    snapshots.create_snapshot()
    cfg = load_config()
    cfg["backup_dir"] = str(tmp_path / "backup" / "other")  # another folder of the same install
    save_config(cfg)
    (tmp_path / "backup" / "other").mkdir()
    assert snapshots.cleanup_status()["pending"] is None
    cfg["backup_dir"] = str(tmp_path.parent / f"{tmp_path.name}-outside")  # outside the install
    save_config(cfg)
    assert snapshots.cleanup_status()["pending"] is None


@pytest.mark.parametrize("value", [1, ["backup/night"], True])
def test_a_damaged_install_binding_is_read_only_unknown(default_night, value):
    snapshots.create_snapshot()
    record = json.loads(snapshots.status_path().read_text(encoding="utf-8"))
    record["location_in_root"] = value
    snapshots.status_path().write_text(json.dumps(record), encoding="utf-8")
    observed = snapshots.cleanup_status()
    assert observed["pending"] is None
    assert observed["read_error"] is True


def test_a_folder_outside_the_install_is_bound_by_its_full_path_only(night):
    snapshots.create_snapshot()
    assert snapshots.status()["location_in_root"] is None
    assert snapshots.cleanup_status()["pending"] is False


def test_deleting_a_copy_rebinds_with_the_install_binding(default_night, tmp_path, tmp_path_factory, monkeypatch):
    first = Path(snapshots.create_snapshot()["snapshot"])
    snapshots.create_snapshot()
    record = json.loads(snapshots.status_path().read_text(encoding="utf-8"))
    record.pop("location_in_root")  # as 1.32.0 wrote it
    snapshots.status_path().write_text(json.dumps(record), encoding="utf-8")
    view = night_create.snapshot_delete_view(first.name)
    night_create.delete_snapshot(first.name, view["revision"])
    assert snapshots.status()["location_in_root"] == "backup/night"
    _move_install(tmp_path, tmp_path_factory, monkeypatch)
    assert snapshots.cleanup_status()["pending"] is False
