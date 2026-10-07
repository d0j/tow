from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
import test_import_monitorrent as fixtures

from tow import import_monitorrent as importer
from tow import site_journal, store, store_transaction
from tow.config import load_config, save_config
from tow.i18n import t
from tow.restore_points import list_restore_points

database = fixtures.database


def _bytes():
    return {
        name: path.read_bytes() if path.is_file() else None for name, path in site_journal.journal_targets().items()
    }


def _state_fault(monkeypatch, write):
    monkeypatch.setattr(store, "save_state", write)
    if hasattr(importer, "save_state"):
        monkeypatch.setattr(importer, "save_state", write)


def test_state_write_failure_restores_every_original_store(database, monkeypatch):
    before = _bytes()

    def fail(_value):
        raise PermissionError("synthetic denied state")

    _state_fault(monkeypatch, fail)
    with pytest.raises(importer.MonitorrentImportError) as caught:
        importer.import_monitorrent(database, apply=True)
    assert caught.value.code == "monitorrent.write_failed"
    assert _bytes() == before
    assert not site_journal.journal_root().exists()
    assert len(list_restore_points()) == 1


def test_silent_state_write_does_not_report_import_success(database, monkeypatch):
    before = _bytes()
    _state_fault(monkeypatch, lambda _value: None)
    with pytest.raises(importer.MonitorrentImportError) as caught:
        importer.import_monitorrent(database, apply=True)
    assert caught.value.code == "monitorrent.write_failed"
    assert _bytes() == before


class Crash(BaseException):
    pass


@pytest.mark.parametrize("step", ["secrets", "state"])
def test_crash_after_either_write_is_recovered_by_next_lock(database, monkeypatch, step):
    before = _bytes()

    def die(name):
        if name == step:
            raise Crash(name)

    monkeypatch.setattr(store_transaction, "_after_write", die)
    with pytest.raises(Crash):
        importer.import_monitorrent(database, apply=True)
    assert site_journal.journal_root().exists()
    monkeypatch.setattr(store_transaction, "_after_write", None)
    with store.persistence_lock():
        assert _bytes() == before
    assert not site_journal.journal_root().exists()
    assert len(list_restore_points()) == 1


def test_rollback_failure_is_critical_and_retains_recovery_evidence(database, monkeypatch):
    recover = site_journal.recover_unlocked
    fault = {"active": False}

    def fail(_value):
        fault["active"] = True
        raise PermissionError("synthetic denied state")

    def fail_recovery(*args, **kwargs):
        if fault["active"]:
            raise PermissionError("synthetic denied rollback")
        return recover(*args, **kwargs)

    _state_fault(monkeypatch, fail)
    monkeypatch.setattr(site_journal, "recover_unlocked", fail_recovery)
    with pytest.raises(importer.MonitorrentImportError) as caught:
        importer.import_monitorrent(database, apply=True)
    assert caught.value.code == "monitorrent.rollback_failed"
    assert site_journal.journal_root().exists()


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_cli_reports_safe_localized_failure_and_no_false_success(database, monkeypatch, capsys, lang):
    from tow import cli

    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)

    def fail(_value):
        raise PermissionError("synthetic-private-error-do-not-print")

    _state_fault(monkeypatch, fail)
    assert cli.main(["import-monitorrent", "--db", str(database), "--apply", "--json"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error"] == t("monitorrent.write_failed", lang)
    assert "synthetic-private-error" not in result["error"]


@pytest.mark.parametrize("kind", ["transmission", "deluge"])
def test_other_client_never_receives_qbittorrent_credentials(database, kind):
    cfg = load_config()
    cfg["client"] = {"kind": kind}
    save_config(cfg)
    before = store.load_secrets()
    result = importer.import_monitorrent(database, apply=True)
    after = store.load_secrets()
    assert after.get(kind) == before.get(kind)
    assert "qbittorrent" not in result["credentials_filled"]
    assert result["credentials_skipped"] == ["qbittorrent"]
    assert store.load_state()["topics"][-1]["client_id"] == "default"


def _clients():
    cfg = load_config()
    cfg["clients"] = [
        {"id": "other", "kind": "transmission", "default": True},
        {"id": "chosen", "kind": "qbittorrent"},
        {"id": "disabled", "kind": "qbittorrent", "enabled": False},
    ]
    save_config(cfg)


def test_explicit_destination_is_bound_to_new_topics_and_only_its_secret_block(database):
    _clients()
    before_topics = store.load_state()["topics"]
    result = importer.import_monitorrent(database, apply=True, client_id="chosen")
    assert result["client_id"] == "chosen"
    assert result["credentials_skipped"] == []
    secrets = store.load_secrets()
    assert set(secrets["clients"]) == {"chosen"}
    assert secrets["clients"]["chosen"]["username"] == "mr-user"
    topics = store.load_state()["topics"]
    assert topics[:-1] == before_topics
    assert topics[-1]["client_id"] == "chosen"


@pytest.mark.parametrize("target", ["disabled", "absent"])
def test_invalid_destination_is_refused_before_safety_archive_or_store_write(database, target):
    _clients()
    before = _bytes()
    with pytest.raises(importer.MonitorrentImportError):
        importer.import_monitorrent(database, apply=True, client_id=target)
    assert _bytes() == before
    assert list_restore_points() == []


def test_cli_can_choose_destination_without_writing_during_preview(database, capsys):
    from tow import cli

    _clients()
    before = _bytes()
    assert cli.main(["import-monitorrent", "--db", str(database), "--client", "chosen", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["preview"] is True
    assert result["client_id"] == "chosen"
    assert _bytes() == before
    assert list_restore_points() == []


def test_corrupt_source_is_not_a_successful_empty_database(tmp_path):
    path = tmp_path / "broken.sqlite"
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(importer.MonitorrentImportError) as caught:
        importer.import_monitorrent(path)
    assert caught.value.code == "monitorrent.invalid_database"
    assert list_restore_points() == []


def test_locked_query_is_not_swallowed_as_a_missing_optional_table():
    def fail(_sql):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        importer._rows(SimpleNamespace(execute=fail), "SELECT id FROM topics")


def test_source_topics_and_credentials_use_one_read_snapshot(database, monkeypatch):
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("PRAGMA journal_mode=WAL")
    original = importer._read_topics

    def concurrent_change(connection):
        topics = original(connection)
        with closing(sqlite3.connect(database)) as writer, writer:
            writer.execute("UPDATE qbittorrent_credentials SET username='concurrent-new-user'")
        return topics

    monkeypatch.setattr(importer, "_read_topics", concurrent_change)
    importer.import_monitorrent(database, apply=True)
    assert store.load_secrets()["qbittorrent"]["username"] == "mr-user"


@pytest.mark.parametrize("silent", [False, True])
def test_secret_write_failure_or_noop_never_commits_new_state(database, monkeypatch, silent):
    before = _bytes()
    original = store.save_secrets

    def fail(value):
        if silent:
            return
        original(value)
        raise PermissionError("synthetic error after secret replacement")

    monkeypatch.setattr(store, "save_secrets", fail)
    with pytest.raises(importer.MonitorrentImportError) as caught:
        importer.import_monitorrent(database, apply=True)
    assert caught.value.code == "monitorrent.write_failed"
    assert _bytes() == before
    assert not site_journal.journal_root().exists()


@pytest.mark.parametrize("phase", ["backup", "journal"])
def test_preparation_failure_is_not_reported_as_a_completed_import(database, monkeypatch, phase):
    from tow import restore_points

    before = _bytes()

    def fail(*_args, **_kwargs):
        raise PermissionError("synthetic prepare denied")

    if phase == "backup":
        monkeypatch.setattr(restore_points, "create_restore_point", fail)
    else:
        monkeypatch.setattr(store_transaction, "begin_unlocked", fail)
    with pytest.raises(importer.MonitorrentImportError) as caught:
        importer.import_monitorrent(database, apply=True)
    assert caught.value.code == f"monitorrent.{'backup' if phase == 'backup' else 'prepare'}_failed"
    assert _bytes() == before
    assert len(list_restore_points()) == (0 if phase == "backup" else 1)


@pytest.mark.parametrize("apply", [False, True])
def test_disabled_or_unknown_client_kind_is_refused_without_writes(database, apply):
    cfg = load_config()
    cfg["client"] = {"kind": "synthetic-unsupported"}
    save_config(cfg)
    before = _bytes()
    with pytest.raises(importer.MonitorrentImportError) as caught:
        importer.import_monitorrent(database, apply=apply)
    assert caught.value.code == "client.factory.not_implemented"
    assert _bytes() == before
    assert list_restore_points() == []


def test_preview_reports_incompatible_credentials_and_import_never_changes_source(database):
    cfg = load_config()
    cfg["client"] = {"kind": "transmission"}
    save_config(cfg)
    source = database.read_bytes()
    preview = importer.import_monitorrent(database)
    assert preview["credentials_skipped"] == ["qbittorrent"]
    applied = importer.import_monitorrent(database, apply=True)
    assert applied["warnings"] == [t("monitorrent.qbittorrent_skipped")]
    assert database.read_bytes() == source


@pytest.mark.parametrize("value", ["not-a-number", None, "1; x"])
def test_a_row_with_a_malformed_id_is_skipped_and_counted(database, value):
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("UPDATE topics SET id=? WHERE id=3", (value,))
    preview = importer.import_monitorrent(database)
    assert (preview["topics_found"], preview["topics_unusable"]) == (3, 1)
    result = importer.import_monitorrent(database, apply=True)
    assert (result["topics_added"], result["topics_unusable"]) == (0, 1)  # 1 and 2 are watched already


def _rows_of(database, *rows):
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("DELETE FROM topics")
        writer.executemany("INSERT INTO topics VALUES (?, ?, ?, ?)", rows)


def test_rows_another_way_in_would_refuse_are_skipped(database):
    """A script link, a site TOW does not read: the .towx import refuses them; so does this one."""
    _rows_of(
        database,
        (10, "Script", "javascript:alert(1)", "/d"),
        (11, "Unknown site", "https://video.example/series/Show_A/", "/d"),
        (12, None, "http://rutor.info/torrent/12/show_a", None),
    )
    result = importer.import_monitorrent(database, apply=True)
    assert (result["topics_added"], result["topics_unusable"]) == (1, 2)
    added = next(topic for topic in store.load_state()["topics"] if topic["id"] == "mr-12")
    # Without a name the link stands in until the first check names it after its torrent.
    assert (added["title"], added["save_path"]) == ("http://rutor.info/torrent/12/show_a", "")


def test_one_title_that_is_not_utf8_skips_its_row_only(database):
    _rows_of(database, (20, "ok", "http://rutor.info/torrent/20/x", "/d"))
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute(
            "INSERT INTO topics VALUES (21, CAST(X'C1E5F0E5E3' AS TEXT), 'http://rutor.info/torrent/21/y', '/d')"
        )
    result = importer.import_monitorrent(database, apply=True)
    assert (result["topics_added"], result["topics_unusable"]) == (1, 1)
    assert "mr-20" in {topic["id"] for topic in store.load_state()["topics"]}


@pytest.mark.parametrize("other_kind", ["transmission", "deluge"])
@pytest.mark.parametrize("apply", [False, True])
def test_shared_reference_across_client_kinds_never_receives_imported_credentials(database, other_kind, apply):
    _clients()
    cfg = load_config()
    cfg["clients"][0].update(kind=other_kind, secrets_ref="shared")
    cfg["clients"][1]["secrets_ref"] = "shared"
    save_config(cfg)
    before = _bytes()
    result = importer.import_monitorrent(database, client_id="chosen", apply=apply)
    assert result["credentials_skipped"] == ["qbittorrent"]
    assert result["warnings"] == [t("monitorrent.shared_credentials")]
    assert "shared" not in store.load_secrets().get("clients", {})
    if apply:
        assert store.load_state()["topics"][-1]["client_id"] == "chosen"
        assert "qbittorrent" not in result["credentials_filled"]
    else:
        assert _bytes() == before
        assert list_restore_points() == []


def test_intentionally_shared_same_kind_credentials_remain_compatible(database):
    _clients()
    cfg = load_config()
    cfg["clients"][0].update(kind="qbittorrent", secrets_ref="shared")
    cfg["clients"][1]["secrets_ref"] = "shared"
    save_config(cfg)
    result = importer.import_monitorrent(database, client_id="chosen", apply=True)
    assert result["credentials_skipped"] == []
    assert store.load_secrets()["clients"]["shared"]["username"] == "mr-user"


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("block", [{"username": "owner"}, {"password": "owner-secret"}, {"port": 8181}])
def test_partly_configured_client_credentials_are_not_replaced(database, modern, block):
    if modern:
        _clients()
        store.save_secrets({"clients": {"chosen": block}})
    else:
        store.save_secrets({"qbittorrent": block})
    result = importer.import_monitorrent(database, client_id="chosen" if modern else None, apply=True)
    after = store.load_secrets()
    assert (after["clients"]["chosen"] if modern else after["qbittorrent"]) == block
    assert "qbittorrent" in result["credentials_kept"]
    assert "qbittorrent" not in result["credentials_filled"]


@pytest.mark.parametrize("field", ["uid", "pass", "username", "password"])
def test_any_existing_tracker_credential_is_kept_without_mixing_accounts(database, field):
    block = {field: "owner-value"}
    store.save_secrets({"trackers": {"kinozal": block}})
    result = importer.import_monitorrent(database, apply=True)
    assert store.load_secrets()["trackers"]["kinozal"] == block
    assert "kinozal" in result["credentials_kept"]


def test_existing_notification_recipients_are_not_replaced_by_import(database):
    store.save_secrets({"telegram": {"chat_ids": ["synthetic-owner-recipient"]}})
    result = importer.import_monitorrent(database, apply=True)
    assert store.load_secrets()["telegram"] == {"chat_ids": ["synthetic-owner-recipient"]}
    assert "telegram" in result["credentials_kept"]


@pytest.mark.parametrize("port", [8080, "8080"])
def test_default_port_alone_does_not_make_an_empty_client_configured(database, port):
    store.save_secrets({"qbittorrent": {"host": "", "username": "", "password": "", "port": port}})
    result = importer.import_monitorrent(database, apply=True)
    assert "qbittorrent" in result["credentials_filled"]
    assert store.load_secrets()["qbittorrent"]["port"] == 8081


HASH_A = "A1" * 20


class _Client:
    """A torrent client holding Monitorrent's torrent, not marked as TOW's."""

    def __init__(self) -> None:
        self.tags: dict[str, list[str]] = {HASH_A: []}

    def inspect_torrent(self, h):
        return {"hash": h, "tags": list(self.tags[h])} if h in self.tags else None

    def adopt_torrent(self, h):
        self.tags[h].append("tow")
        return self.tags[h]


def _with_known_hash(database, monkeypatch):
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("INSERT INTO topics VALUES (30, 'Known', 'http://rutor.info/torrent/30/known', 'M:\\new')")
        writer.execute("INSERT INTO topics VALUES (31, 'Gone', 'http://rutor.info/torrent/31/gone', 'M:\\new')")
        writer.execute("INSERT INTO rutororg_topics VALUES (30, ?)", (HASH_A,))
        writer.execute("INSERT INTO rutororg_topics VALUES (31, ?)", ("B2" * 20,))
    client = _Client()
    monkeypatch.setattr("tow.adopt.client_factory.from_secrets", lambda cfg, secrets, client_id=None: client)
    return client


def test_import_adopts_existing_torrents_only_when_asked(database, monkeypatch):
    client = _with_known_hash(database, monkeypatch)
    assert importer.import_monitorrent(database, adopt=True)["adopt_candidates"] == 2  # a preview asks nothing
    assert client.tags[HASH_A] == []
    result = importer.import_monitorrent(database, apply=True, adopt=True)
    assert result["adopted"] == ["mr-30"]  # mr-31's torrent is not in the client: its first check adds it
    assert (result["adopt_failed"], result["adopt_after_check"]) == (0, 1)  # mr-3: no usable hash
    assert client.tags[HASH_A] == ["tow"]


def test_import_without_adopt_never_asks_the_client(database, monkeypatch):
    client = _with_known_hash(database, monkeypatch)
    monkeypatch.setattr("tow.adopt.client_factory.from_secrets", lambda *a, **k: pytest.fail("client asked"))
    result = importer.import_monitorrent(database, apply=True)
    assert "adopted" not in result
    assert client.tags[HASH_A] == []
