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


def test_a_wrong_value_is_named_in_the_owners_language():
    from tow.config import ConfigError
    from tow.i18n import t

    _write("port: http\n")
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert caught.value.code == "config_error.whole_number"
    assert caught.value.text("ru") == t("config_error.whole_number", "ru", key="port", low=1, high=65535)
    assert "целым числом" in caught.value.text("ru")


@pytest.mark.parametrize(
    ("text", "code", "values"),
    [
        ("port: 8790\nbind: [127.0.0.1\n", "config_error.syntax", {"line": 3, "column": 1}),
        ("a: b\n  c: d\n", "config_error.syntax", {"line": 2, "column": 4}),
        ("- port\n", "config_error.mapping", {}),
        ("just text\n", "config_error.mapping", {}),
    ],
)
def test_unreadable_yaml_or_a_file_without_settings_is_a_config_error(text, code, values):
    # Both used to escape as a bare yaml.YAMLError / TypeError: "the command could not be completed".
    from tow.config import ConfigError

    _write(text)
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert (caught.value.code, {k: v for k, v in caught.value.params.items() if k != "_prefix"}) == (code, values)


@pytest.mark.parametrize(
    ("setting", "code", "key"),
    [
        ("title: [Show]", "config_error.site_text", "title"),
        ("title: true", "config_error.site_text", "title"),
        ("login_path: [/login]", "config_error.site_text", "login_path"),
        ('browser_auth: "maybe"', "config_error.site_true_false", "browser_auth"),
        ("page_download: 2", "config_error.site_true_false", "page_download"),
        ("page_download: {on: true}", "config_error.site_true_false", "page_download"),
        ('fail_threshold: "three"', "config_error.site_number", "fail_threshold"),
        ("fail_threshold: 1.5", "config_error.site_number", "fail_threshold"),
        ("cooldown_sec: true", "config_error.site_number", "cooldown_sec"),
        ("cooldown_sec: [1800]", "config_error.site_number", "cooldown_sec"),
        ("fetch_hosts: tracker.example", "config_error.site_hosts", "fetch_hosts"),
        ("cookie_names: uid", "config_error.site_text_list", "cookie_names"),
        ("cookie_names: [1]", "config_error.site_text_list", "cookie_names"),
        ("login_form: {password: x}", "config_error.site_login_form", None),
        ("login_form: {extra: {login: 1}}", "config_error.site_login_form", None),
        ("login_form: [user]", "config_error.site_login_form", None),
    ],
)
def test_a_hand_edited_site_setting_of_the_wrong_type_is_refused_at_load(setting, code, key):
    # These were checked only when a .towx was imported; a hand-edited config.yaml with
    # `browser_auth: "false"` loaded and turned the browser sign-in on.
    from tow.config import ConfigError
    from tow.i18n import t

    hosts = "" if setting.startswith("fetch_hosts") else "    fetch_hosts: [https://tracker.example]\n"
    _write(f"trackers:\n  site:\n{hosts}    {setting}\n")
    with pytest.raises(ConfigError) as caught:
        load_config()
    params = {"site": "site", **({"key": key} if key else {})}
    assert (caught.value.code, caught.value.params) == (code, params)
    assert caught.value.text("ru") == t(code, "ru", **params)


@pytest.mark.parametrize(
    ("setting", "key", "value"),
    [
        ("cooldown_sec: '1800'", "cooldown_sec", 1800),
        ("cooldown_sec: 1800.0", "cooldown_sec", 1800),
        ("fail_threshold: ' 3 '", "fail_threshold", 3),
        ("browser_auth: 'true'", "browser_auth", True),
        ('browser_auth: "false"', "browser_auth", False),
        ("page_download: 1", "page_download", True),
        ("page_download: 0", "page_download", False),
        ("title: 2024", "title", "2024"),
        ("fetch_hosts: https://other.example", "fetch_hosts", ["https://other.example"]),
    ],
)
def test_a_loosely_written_site_setting_loads_as_what_it_means(setting, key, value):
    # 1.23 read these with int() and truthiness and started; 1.24.0 refused them at load, so an
    # update stopped at the new version's start and rolled back. ('false' was even read as on.)
    hosts = "" if key == "fetch_hosts" else "    fetch_hosts: [https://tracker.example]\n"
    _write(f"trackers:\n  site:\n{hosts}    {setting}\n")
    site = load_config()["trackers"]["site"]
    assert site[key] == value
    assert type(site[key]) is type(value)


def test_every_known_site_and_the_example_config_still_load():
    from pathlib import Path

    from tow.config import validated
    from tow.trackers.presets import known_sites

    example = Path(__file__).parents[1] / "config.example.yaml"
    _write(example.read_text(encoding="utf-8"))
    assert load_config()["trackers"]
    assert validated({"trackers": known_sites()})["trackers"].keys() == known_sites().keys()
    site = {
        "title": None,  # empty settings mean "not set", as before
        "browser_auth": None,
        "fail_threshold": None,
        "login_form": {"user_field": "login", "pw_field": None, "extra": {"login": "Вход"}},
        "cookie_names": [],
    }
    assert validated({"trackers": {"site": site}})["trackers"]["site"] == site


def test_the_bundle_check_and_the_loader_share_one_schema():
    # An imported bundle used to check a state key (save_roots) and never allowed_save_roots.
    from tow.bundle import ExportImportError, _validate_config_schema
    from tow.config import validated

    good = {"allowed_save_roots": ["D:/media"], "allow_unc_save_paths": "yes"}
    assert validated(good)["allowed_save_roots"] == ["D:/media"]
    assert "interval_sec" not in good  # the caller's mapping is not filled in
    _validate_config_schema(good)
    with pytest.raises(ExportImportError, match="allowed_save_roots must be a list of folders") as caught:
        _validate_config_schema({"allowed_save_roots": "D:/media"})
    # The log keeps the English text; `tow import` names the same setting in the owner's language.
    from tow.i18n import t

    reason = t("config_error.folder_list", "ru", key="allowed_save_roots")
    assert caught.value.owner_text.text("ru") == t("cli.bundle.import_refused", "ru", reason=reason)


def test_the_interval_and_the_port_have_one_default():
    from tow.config import DEFAULTS, interval_sec_of, port_of

    assert (interval_sec_of({}), port_of({})) == (DEFAULTS["interval_sec"], DEFAULTS["port"]) == (3600, 8787)
    assert (interval_sec_of({"interval_sec": 900}), port_of({"port": 9100})) == (900, 9100)
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
