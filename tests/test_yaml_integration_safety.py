from __future__ import annotations

import pytest
import yaml

from tow import bundle, cli, config, i18n, snapshots
from tow.paths import config_path

CYCLE = b"extra: &loop [*loop]\n"


def test_loading_a_cyclic_config_is_refused_before_defaults_are_applied():
    config_path().write_bytes(CYCLE)
    with pytest.raises(yaml.YAMLError):
        config.load_config()
    assert config_path().read_bytes() == CYCLE


def test_a_programmatic_cycle_cannot_be_written_as_a_new_config():
    before = config_path().read_bytes()
    cycle = []
    cycle.append(cycle)
    with pytest.raises(yaml.YAMLError):
        config.save_config({"extra": cycle})
    assert config_path().read_bytes() == before


def test_programmatic_validation_refuses_a_cycle_before_deepcopy():
    cycle = {}
    cycle["child"] = cycle
    with pytest.raises(yaml.YAMLError):
        config.validated({"extra": cycle})


def test_an_in_place_edit_cannot_compare_or_save_a_cyclic_config():
    config_path().write_bytes(CYCLE)
    with pytest.raises(yaml.YAMLError):
        config.set_interval_sec(600)
    assert config_path().read_bytes() == CYCLE


def test_bundle_yaml_is_guarded_before_recursive_secret_or_schema_checks():
    with pytest.raises(bundle.ExportImportError):
        bundle._parse_mapping(CYCLE, label="config.yaml")


def test_export_refuses_cyclic_yaml_before_looking_for_plaintext_secrets(tmp_path, monkeypatch):
    config_path().write_bytes(CYCLE)
    output = tmp_path / "unsafe.towx"
    monkeypatch.setattr(bundle, "_secret_key_path", lambda *args: pytest.fail("unsafe graph reached recursive scan"))
    with pytest.raises(bundle.ExportImportError):
        bundle.export_bundle(output, "synthetic-passphrase")
    assert not output.exists()
    assert config_path().read_bytes() == CYCLE


def test_night_restore_config_is_refused_before_serialization():
    with pytest.raises(snapshots.SnapshotError):
        snapshots._keep_local_access(CYCLE)


def test_night_rollback_config_is_guarded_before_resolving_restore_point_targets(monkeypatch):
    from tow import locations

    monkeypatch.setattr(locations, "resolve_checked", lambda *args: pytest.fail("unsafe graph reached path resolver"))
    with pytest.raises(yaml.YAMLError):
        snapshots._rollback_target("restore-points/20260101T010101Z-aaaaaaaa.towx", CYCLE, None)


@pytest.mark.parametrize("source", [b"false", b"0", b"''", b"[]"])
def test_falsy_non_mapping_configs_are_not_silently_replaced_by_defaults(source):
    config_path().write_bytes(source)
    with pytest.raises(config.ConfigError) as caught:
        config.load_config()
    assert caught.value.code == "config_error.mapping"
    with pytest.raises(snapshots.SnapshotError):
        snapshots._snapshot_config(source)
    assert config_path().read_bytes() == source


@pytest.mark.parametrize("source", [b"", b"# comment only", b"null", b"{}"])
def test_empty_mapping_and_empty_document_remain_compatible(source):
    config_path().write_bytes(source)
    assert isinstance(config.load_config(), dict)
    assert snapshots._snapshot_config(source) == {}


@pytest.mark.parametrize("language", ["ru", "en"])
def test_cli_bad_yaml_has_a_localized_message_not_a_traceback_or_private_values(language, capsys, monkeypatch):
    monkeypatch.delenv("TOW_DEBUG", raising=False)
    config_path().write_bytes(b"private-marker: &loop [*loop]\n")
    monkeypatch.setattr(i18n, "_CURRENT", i18n.ContextVar("test_language", default=None))
    monkeypatch.setattr(cli, "_use_language", lambda _argv: i18n.use(language))
    assert cli.main(["doctor", "--json"]) == cli.EXIT_CANNOT_RUN
    output = capsys.readouterr()
    assert i18n.translate("yaml_limits.references", language) in output.out
    assert "private-marker" not in output.out + output.err
    assert "Traceback" not in output.out + output.err


def test_error_rendering_with_a_broken_config_cannot_recursively_render_itself(monkeypatch, caplog):
    from tow.yaml_guard import YamlLimitError

    monkeypatch.setattr(i18n, "_CURRENT", i18n.ContextVar("test_language", default=None))
    monkeypatch.setattr(i18n, "_TOLD", set())
    i18n._configured_language_of.cache_clear()
    config_path().write_bytes(CYCLE)
    assert str(YamlLimitError("yaml_limits.references"))
    told = [entry.getMessage() for entry in caplog.records if entry.name == "tow.i18n"]
    assert len(told) == 1
    assert "config.yaml" in told[0]


def test_healthy_restore_merge_can_repair_a_cyclic_live_config_without_mutating_it():
    config_path().write_bytes(CYCLE)
    restored = yaml.safe_load(snapshots._keep_local_access(b"interval_sec: 600\nbind: 0.0.0.0\nallow_lan: true\n"))
    assert restored["interval_sec"] == 600
    assert restored["bind"] == "127.0.0.1"
    assert restored["allow_lan"] is False
    assert config_path().read_bytes() == CYCLE


def test_secret_scan_preserves_first_match_and_path_prefix():
    shared = {"ordinary": [{"password": "synthetic"}]}
    assert bundle._secret_key_path({"first": shared, "second": shared}, "config") == "config.first.ordinary[0].password"
    assert bundle._secret_key_path({"token": False, "api_key": "synthetic"}) == "api_key"


def test_secret_scan_is_iterative_and_handles_shared_cycles():
    value = {"token": "synthetic"}
    for _ in range(2000):
        value = {"child": value}
    found = bundle._secret_key_path(value)
    assert found == "child." * 2000 + "token"
    cycle = []
    cycle.append(cycle)
    assert bundle._secret_key_path(cycle) is None


def test_secret_scan_does_not_apply_yaml_node_limits_to_large_json_history(monkeypatch):
    from tow import yaml_guard

    monkeypatch.setattr(yaml_guard, "MAX_EXPANDED_NODES", 5)
    assert bundle._secret_key_path({"history": [{"episode": number} for number in range(50)]}) is None


def test_saving_multibyte_text_cannot_create_a_config_above_its_read_limit(tmp_path, monkeypatch):
    from tow import yaml_guard

    target = tmp_path / "config.yaml"
    target.write_bytes(b"{}\n")
    monkeypatch.setattr(yaml_guard, "MAX_INPUT_BYTES", 128)
    before = target.read_bytes()
    with pytest.raises(yaml_guard.YamlLimitError):
        config.save_config({"extra": "я" * 60})
    assert target.read_bytes() == before


def test_restoration_cannot_expand_scalar_aliases_into_an_unreadable_config(tmp_path, monkeypatch):
    from tow import yaml_guard

    target = tmp_path / "config.yaml"
    target.write_bytes(b"{}\n")
    monkeypatch.setattr(yaml_guard, "MAX_INPUT_BYTES", 128)
    source = ("a: &a " + "я" * 20 + "\nb: [*a, *a, *a, *a]\n").encode()
    assert len(source) < 128
    with pytest.raises(snapshots.SnapshotError):
        snapshots._keep_local_access(source)
    assert target.read_bytes() == b"{}\n"


def test_oversized_serialization_does_not_create_a_new_destination_directory(tmp_path, monkeypatch):
    from tow import yaml_guard

    target = tmp_path / "new-folder" / "config.yaml"
    monkeypatch.setenv("TOW_CONFIG", str(target))
    monkeypatch.setattr(yaml_guard, "MAX_INPUT_BYTES", 128)
    with pytest.raises(yaml_guard.YamlLimitError):
        config.save_config({"extra": "я" * 60})
    assert not target.parent.exists()
