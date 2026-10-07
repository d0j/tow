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


# --- Monitorrent databases of every age and every site plugin --------------------------------

_MODERN = """
CREATE TABLE topics (id INTEGER PRIMARY KEY, display_name VARCHAR, url VARCHAR, type VARCHAR,
  paused BOOLEAN NOT NULL DEFAULT 0, download_dir VARCHAR);
CREATE TABLE rutracker_topics (id INTEGER PRIMARY KEY, hash VARCHAR);
CREATE TABLE nnmclub_topics (id INTEGER PRIMARY KEY, hash VARCHAR);
CREATE TABLE tapochek_topics (id INTEGER PRIMARY KEY, hash VARCHAR);
CREATE TABLE kinozal_topics (id INTEGER PRIMARY KEY, hash VARCHAR);
CREATE TABLE lostfilmtv_topics (id INTEGER PRIMARY KEY, season INTEGER);
CREATE TABLE transmission_credentials (host VARCHAR, port INTEGER, username VARCHAR, password VARCHAR);
"""
_H = "ABCDEF0123456789ABCDEF0123456789ABCDEF0"


def _monitorrent(tmp_path, monkeypatch, schema, *rows):
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    path = tmp_path / "monitorrent.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        for sql, args in rows:
            connection.execute(sql, args)
    connection.close()
    save_state({"topics": [], "mirrors": {}})
    save_secrets({})
    return path


def test_every_site_plugin_gives_its_hashes(tmp_path, monkeypatch):
    sites = [
        (1, "rutracker_topics", "https://rutracker.org/forum/viewtopic.php?t=101"),
        (2, "nnmclub_topics", "https://nnmclub.to/forum/viewtopic.php?t=102"),
        (3, "tapochek_topics", "https://tapochek.net/viewtopic.php?t=103"),
        (4, "kinozal_topics", "https://kinozal.tv/details.php?id=104"),
    ]
    rows = []
    for tid, table, url in sites:
        rows.append(
            ("INSERT INTO topics (id, display_name, url, download_dir) VALUES (?, ?, ?, '/d')", (tid, f"T{tid}", url))
        )
        rows.append((f"INSERT INTO {table} (id, hash) VALUES (?, ?)", (tid, (_H + str(tid)).lower())))
    path = _monitorrent(tmp_path, monkeypatch, _MODERN, *rows)
    assert import_monitorrent(path, adopt=True)["adopt_candidates"] == 4
    import_monitorrent(path, apply=True)
    assert {t["id"]: t["hash"] for t in load_state()["topics"]} == {f"mr-{n}": _H + str(n) for n in range(1, 5)}


def test_an_old_database_without_download_dir_or_paused_still_imports(tmp_path, monkeypatch):
    schema = "CREATE TABLE topics (id INTEGER PRIMARY KEY, display_name VARCHAR, url VARCHAR, type VARCHAR);"
    path = _monitorrent(
        tmp_path,
        monkeypatch,
        schema,
        ("INSERT INTO topics VALUES (1, 'Show', 'http://rutor.info/torrent/1/x', 'rutor.org')", ()),
    )
    assert import_monitorrent(path)["topics_new"] == 1
    import_monitorrent(path, apply=True)
    assert load_state()["topics"][0]["save_path"] == ""


def test_a_paused_monitorrent_topic_stays_paused(tmp_path, monkeypatch):
    path = _monitorrent(
        tmp_path,
        monkeypatch,
        _MODERN,
        ("INSERT INTO topics (id, display_name, url, paused) VALUES (1, 'P', 'http://rutor.info/torrent/7/x', 1)", ()),
        ("INSERT INTO topics (id, display_name, url, paused) VALUES (2, 'A', 'http://rutor.info/torrent/8/x', 0)", ()),
    )
    import_monitorrent(path, apply=True)
    assert {t["id"]: bool(t.get("paused")) for t in load_state()["topics"]} == {"mr-1": True, "mr-2": False}


@pytest.mark.parametrize(
    ("kept", "now"),
    [
        ("http://nnm-club.me/forum/viewtopic.php?t=1", "https://nnmclub.to/forum/viewtopic.php?t=1"),
        ("https://nnm-club.to/forum/viewtopic.php?t=1", "https://nnmclub.to/forum/viewtopic.php?t=1"),
        ("http://www.rutor.org/torrent/1/x", "http://rutor.info/torrent/1/x"),
    ],
)
def test_an_address_the_site_has_left_becomes_todays(tmp_path, monkeypatch, kept, now):
    path = _monitorrent(
        tmp_path, monkeypatch, _MODERN, ("INSERT INTO topics (id, display_name, url) VALUES (1, 'S', ?)", (kept,))
    )
    import_monitorrent(path, apply=True)
    assert load_state()["topics"][0]["url"] == now


def test_a_topic_of_a_site_tow_does_not_read_is_named(tmp_path, monkeypatch):
    path = _monitorrent(
        tmp_path,
        monkeypatch,
        _MODERN,
        ("INSERT INTO topics (id, display_name, url) VALUES (1, 'Lost show', 'https://lostfilm.example/series/x')", ()),
    )
    preview = import_monitorrent(path)
    assert (preview["topics_unusable"], preview["topics_unusable_names"]) == (1, ["Lost show"])


@pytest.mark.parametrize("port", ["abc", "8080/"])
def test_a_port_that_is_not_a_number_skips_only_that_connection(tmp_path, monkeypatch, port):
    schema = (
        _MODERN
        + "CREATE TABLE qbittorrent_credentials (host VARCHAR, port VARCHAR, username VARCHAR, password VARCHAR);"
    )
    path = _monitorrent(
        tmp_path,
        monkeypatch,
        schema,
        ("INSERT INTO topics (id, display_name, url) VALUES (1, 'S', 'http://rutor.info/torrent/1/x')", ()),
        ("INSERT INTO qbittorrent_credentials VALUES ('h', ?, 'u', 'p')", (port,)),
    )
    result = import_monitorrent(path, apply=True)
    assert result["topics_added"] == 1
    assert result["credentials_skipped"] == ["qbittorrent"]
    assert "not a number" in " ".join(result["warnings"]) or "не числом" in " ".join(result["warnings"])
    assert not load_secrets().get("qbittorrent", {}).get("host")


def test_transmission_connection_goes_to_a_transmission_client_only(tmp_path, monkeypatch):
    from tow.clients.factory import client_secret_block
    from tow.config import load_config, save_config

    path = _monitorrent(
        tmp_path,
        monkeypatch,
        _MODERN,
        ("INSERT INTO transmission_credentials VALUES ('nas', 9091, 'tu', 'tp')", ()),
    )
    preview = import_monitorrent(path)
    assert (preview["credentials_found"], preview["credentials_skipped"]) == (["transmission"], ["transmission"])
    assert preview["warnings"]  # said, not silently left out
    cfg = load_config()
    cfg["client"] = {"kind": "transmission"}
    save_config(cfg)
    result = import_monitorrent(path, apply=True)
    assert result["credentials_filled"] == ["transmission"]
    block = client_secret_block(load_config(), load_secrets(), None)
    assert (block["host"], block["port"], block["username"], block["password"]) == ("nas", 9091, "tu", "tp")
