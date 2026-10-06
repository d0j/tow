from __future__ import annotations

import pytest
import yaml

from tow.config import load_config, set_flash_ttl_sec, set_interval_sec
from tow.paths import config_path


def _write(text: str) -> None:
    config_path().write_text(text, encoding="utf-8")


def test_empty_value_does_not_swallow_the_next_key():
    _write("interval_sec:\nport: 8790\nbind: 127.0.0.1\n")

    set_interval_sec(600)

    data = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    assert data == {"interval_sec": 600, "port": 8790, "bind": "127.0.0.1"}


def test_comments_and_layout_survive_an_in_place_edit():
    _write("# owner notes\ninterval_sec: 3600  # twice a day\nport: 8790\n")

    set_interval_sec(43200)

    assert (
        config_path().read_text(encoding="utf-8") == "# owner notes\ninterval_sec: 43200  # twice a day\nport: 8790\n"
    )


def test_missing_key_is_appended_without_touching_the_rest():
    _write("# keep me\nport: 8790\n")

    set_flash_ttl_sec(90)

    assert config_path().read_text(encoding="utf-8") == "# keep me\nport: 8790\nflash_ttl_sec: 90\n"


def test_unusual_layout_falls_back_to_a_verified_full_rewrite():
    # A block value cannot be replaced on one line: the verified fallback rewrites safely.
    _write("interval_sec:\n  nested: 1\nport: 8790\n")

    set_interval_sec(600)

    data = load_config()
    assert data["interval_sec"] == 600
    assert data["port"] == 8790


@pytest.mark.parametrize(("given", "stored"), [(1, 15), (90, 90), (99999, 3600)])
def test_flash_ttl_is_clamped(given, stored):
    set_flash_ttl_sec(given)

    assert load_config()["flash_ttl_sec"] == stored


def test_c_loader_parses_the_example_config_like_the_pure_loader():
    from tow.config import _SAFE_LOADER

    text = config_path().read_text(encoding="utf-8")
    assert yaml.load(text, Loader=_SAFE_LOADER) == yaml.safe_load(text)


def test_cached_config_is_a_private_copy_and_follows_writes():
    first = load_config()
    first["trackers"]["mutated"] = True
    assert "mutated" not in load_config()["trackers"]

    set_interval_sec(777)
    assert load_config()["interval_sec"] == 777

    _write("interval_sec: 300\n")  # an owner's editor, outside TOW
    assert load_config()["interval_sec"] == 300


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("port: http", "port must be a whole number"),
        ("port: 70000", "port must be a whole number"),
        ("interval_sec: [1]", "interval_sec must be a whole number"),
        ("bind: 8", "bind must be a text address"),
        ("trackers: [rutor]", "trackers must map"),
        ("client: qbittorrent", "client must be a mapping"),
        ("allowed_save_roots: D:/media", "allowed_save_roots must be a list of folders"),
        ("allowed_save_roots: [1]", "allowed_save_roots must be a list of folders"),
        ("allow_unc_save_paths: maybe", "allow_unc_save_paths must be true or false"),
        ("allow_private_tracker_hosts: [1]", "allow_private_tracker_hosts must be true or false"),
        ("restore_points_dir: [x]", "restore_points_dir must be a folder path"),
    ],
)
def test_wrong_types_are_named_when_the_config_is_loaded(line, message):
    # N12: a wrong type used to crash much later, far from the config.
    from tow.config import ConfigError

    _write(f"{line}\n")

    with pytest.raises(ConfigError, match=message):
        load_config()


def test_the_bundle_check_and_the_loader_share_one_schema():
    # An imported bundle used to check a state key (save_roots) and never allowed_save_roots.
    from tow.bundle import ExportImportError, _validate_config_schema
    from tow.config import validated

    good = {"allowed_save_roots": ["D:/media"], "allow_unc_save_paths": "yes"}
    assert validated(good)["allowed_save_roots"] == ["D:/media"]
    assert "interval_sec" not in good  # the caller's mapping is not filled in
    _validate_config_schema(good)
    with pytest.raises(ExportImportError, match="allowed_save_roots must be a list of folders"):
        _validate_config_schema({"allowed_save_roots": "D:/media"})


def test_the_interval_and_the_port_have_one_default():
    from tow.config import DEFAULTS, interval_minutes, interval_sec_of, port_of

    assert (interval_sec_of({}), port_of({})) == (DEFAULTS["interval_sec"], DEFAULTS["port"]) == (3600, 8787)
    assert (interval_sec_of({"interval_sec": 900}), port_of({"port": 9100})) == (900, 9100)
    assert interval_minutes(None) == 60
    loaded = load_config()
    assert (interval_sec_of(loaded), port_of(loaded)) == (loaded["interval_sec"], loaded["port"])


def test_numbers_written_as_text_are_accepted():
    _write('port: "8790"\ninterval_sec: "3600"\n')

    data = load_config()

    assert (data["port"], data["interval_sec"]) == (8790, 3600)


@pytest.mark.parametrize(("raw", "enabled"), [("false", False), ("'false'", False), ("no", False), ("true", True)])
def test_client_enabled_flag_written_as_text_is_respected(raw, enabled):
    from tow.clients.factory import client_configurations

    rows = client_configurations({"clients": {"qb": {"kind": "qbittorrent", "enabled": yaml.safe_load(raw)}}})

    assert rows[0]["enabled"] is enabled


@pytest.mark.parametrize(
    ("line", "message"),
    [("quiet_hours: night", "quiet_hours must look like"), ("daily_digest_hour: 25", "daily_digest_hour")],
)
def test_notification_settings_are_validated(line, message):
    from tow.config import ConfigError

    _write(f"{line}\n")

    with pytest.raises(ConfigError, match=message):
        load_config()


def test_saving_does_not_write_defaults_the_file_did_not_have():
    import yaml

    from tow.config import load_config, save_config
    from tow.paths import config_path

    config_path().write_text("trackers: {}\ninterval_sec: 7200\n", encoding="utf-8")
    cfg = load_config()
    cfg["interval_sec"] = 3600 * 3
    save_config(cfg)
    text = config_path().read_text(encoding="utf-8")
    assert text.startswith("# TOW config.")
    assert yaml.safe_load(text) == {"trackers": {}, "interval_sec": 10800}  # no bind/port/client/... added


def test_a_saved_config_points_to_the_commented_reference(monkeypatch, tmp_path):
    from tow import paths
    from tow.config import load_config, save_config
    from tow.paths import config_path

    monkeypatch.setenv("TOW_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path / "app")
    (tmp_path / "config.yaml").write_text("# a comment the UI cannot keep\ninterval_sec: 7200\n", encoding="utf-8")

    save_config(load_config())
    text = config_path().read_text(encoding="utf-8")

    assert "Every setting, explained and with its default: app/config.example.yaml" in text.splitlines()[1]


def test_a_clients_list_drops_the_single_client_block():
    import yaml

    from tow.config import load_config, save_config
    from tow.paths import config_path

    config_path().write_text("trackers: {}\nclient: {kind: qbittorrent}\n", encoding="utf-8")
    cfg = load_config()
    cfg["clients"] = [{"id": "default", "kind": "qbittorrent", "default": True}]
    save_config(cfg)
    assert "client" not in yaml.safe_load(config_path().read_text(encoding="utf-8"))


def test_the_obsolete_lan_auth_flag_is_read_ignored_and_dropped_on_save():
    """lan_auth no longer switches anything (the network always needs the password): an older
    config.yaml that has it still loads, and the next save leaves it out."""
    from tow.config import DEFAULTS, save_config
    from tow.paths import repo_root

    path = config_path()
    path.write_text(path.read_text(encoding="utf-8") + "lan_auth: true\n", encoding="utf-8")

    cfg = load_config()
    assert "lan_auth" not in cfg
    save_config(cfg)
    assert "lan_auth" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "lan_auth" not in DEFAULTS
    assert "lan_auth" not in yaml.safe_load((repo_root() / "config.example.yaml").read_text(encoding="utf-8"))


def test_load_config_hands_out_copies():
    from tow.config import load_config

    first = load_config()
    first["trackers"]["evil"] = {"url_regex": "x"}  # changed without saving
    assert "evil" not in load_config()["trackers"]
