import sqlite3

import pytest
from cryptography.fernet import Fernet

from tow.import_monitorrent import MonitorrentImportError, import_monitorrent
from tow.restore_points import list_restore_points
from tow.store import load_secrets, load_state, save_secrets, save_state

_SCHEMA = """
CREATE TABLE topics (id INTEGER, display_name TEXT, url TEXT, download_dir TEXT);
CREATE TABLE kinozal_topics (id INTEGER, hash TEXT);
CREATE TABLE rutororg_topics (id INTEGER, hash TEXT);
CREATE TABLE qbittorrent_credentials (host TEXT, port INTEGER, username TEXT, password TEXT);
CREATE TABLE kinozal_credentials (c_uid TEXT, c_pass TEXT, username TEXT, password TEXT);
CREATE TABLE telegram_settings (chat_ids TEXT, access_token TEXT);
INSERT INTO topics VALUES (1, 'Imported', 'http://rutor.info/torrent/1/show', 'M:\\new');
INSERT INTO topics VALUES (2, 'Same id', 'http://rutor.info/torrent/2/other', 'M:\\new');
INSERT INTO topics VALUES (3, 'Fresh', 'http://rutor.info/torrent/3/fresh', 'M:\\new');
INSERT INTO rutororg_topics VALUES (3, 'abcdef');
INSERT INTO qbittorrent_credentials VALUES ('192.168.1.5', 8081, 'mr-user', 'mr-pass');
INSERT INTO kinozal_credentials VALUES ('uid', 'pass', 'kz', 'kzpass');
INSERT INTO telegram_settings VALUES ('1, 2', 'mr-token');
"""


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    path = tmp_path / "monitorrent.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.commit()
    connection.close()
    save_state(
        {
            "topics": [
                {
                    "id": "mr-1",
                    "title": "Old",
                    "url": "http://rutor.info/torrent/1/show",
                    "save_path": r"M:\old",
                    "hash": "A" * 40,
                    "client_id": "secondary",
                    "paused": True,
                    "selection": {"mode": "episodes", "value": "S01E03"},
                    "tracking_mode": "once",
                    "once_done": True,
                },
                {"id": "mr-2", "title": "Local with the same id", "url": "http://rutor.info/torrent/99/x"},
            ],
            "mirrors": {},
        }
    )
    save_secrets({"telegram": {"token": "tow-token", "chat_ids": ["9"]}})
    return path


def test_without_apply_it_is_only_a_preview(database):
    before = load_state()

    result = import_monitorrent(database)

    assert result["preview"] is True
    assert (result["topics_found"], result["topics_new"], result["topics_already_watched"]) == (3, 1, 2)
    assert result["credentials_found"] == ["kinozal", "qbittorrent", "telegram"]
    assert load_state() == before
    assert list_restore_points() == []


def test_apply_adds_only_new_topics_after_a_restore_point(database):
    # N8: de-duplicated by id AND url; existing topics keep everything; a restore point first.
    result = import_monitorrent(database, apply=True)

    assert result["topics_added"] == 1
    assert [point["id"] for point in list_restore_points()] == [result["restore_point"]]
    topics = load_state()["topics"]
    assert [t["id"] for t in topics] == ["mr-1", "mr-2", "mr-3"]
    assert topics[0]["title"] == "Old"
    assert topics[0]["selection"] == {"mode": "episodes", "value": "S01E03"}
    assert topics[0]["paused"] is True
    assert topics[1]["url"] == "http://rutor.info/torrent/99/x"
    assert topics[2]["hash"] == "ABCDEF"
    assert topics[2]["tracking_mode"] == "watch"


def test_credentials_fill_empty_slots_and_never_overwrite(database):
    result = import_monitorrent(database, apply=True)

    secrets = load_secrets()
    assert result["credentials_filled"] == ["kinozal", "qbittorrent"]
    assert result["credentials_kept"] == ["telegram"]
    assert secrets["telegram"]["token"] == "tow-token"
    assert secrets["qbittorrent"]["host"] == "192.168.1.5"
    assert secrets["trackers"]["kinozal"]["username"] == "kz"


def test_a_configured_qbit_is_kept(database):
    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "me", "password": "x"}})

    result = import_monitorrent(database, apply=True)

    assert "qbittorrent" in result["credentials_kept"]
    assert load_secrets()["qbittorrent"]["host"] == "127.0.0.1"


def test_missing_tables_and_missing_file(tmp_path):
    partial = tmp_path / "partial.sqlite"
    connection = sqlite3.connect(partial)
    connection.execute("CREATE TABLE topics (id INTEGER, display_name TEXT, url TEXT, download_dir TEXT)")
    connection.commit()
    connection.close()

    assert import_monitorrent(partial)["credentials_found"] == []
    with pytest.raises(MonitorrentImportError) as caught:
        import_monitorrent(tmp_path / "absent.sqlite")
    assert caught.value.code == "monitorrent.missing"


def test_cli_previews_unless_apply(database, capsys):
    from tow import cli

    assert cli.main(["import-monitorrent", "--db", str(database), "--json"]) == 0
    assert '"preview": true' in capsys.readouterr().out
    assert cli.main(["import-monitorrent", "--db", str(database.parent / "nope.sqlite")]) == 3
