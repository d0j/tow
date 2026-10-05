from __future__ import annotations

import base64
import io
import json
import multiprocessing
import os
import zipfile
from pathlib import Path

import pytest
import yaml
from helpers import reaped

from tow.bundle import (
    ExportImportError,
    _atomic_write,
    _contains_secret_keys,
    _json_bytes,
    _parse_mapping,
    _read_bundle,
    _validate_config_schema,
    _validate_history_schema,
    _validate_state_schema,
    _validate_tree,
    _validated_payload,
    _zip_payload,
    export_bundle,
    import_bundle,
    rollback_import,
)
from tow.log import log_event
from tow.paths import config_path, state_path
from tow.store import (
    load_secret_undo,
    load_secrets,
    load_state,
    persistence_lock,
    save_download_history,
    save_secret_undo,
    save_secrets,
    save_state,
)


def _key(seed: bytes = b"test-only-tow-master-key-32bytes") -> str:
    return base64.urlsafe_b64encode(seed).decode()


def _import_worker(bundle: str, home: str, config: str, key: str, started, result) -> None:
    os.environ["TOW_HOME"] = home
    os.environ["TOW_CONFIG"] = config
    os.environ["TOW_MASTER_KEY"] = key
    started.set()
    try:
        imported = import_bundle(Path(bundle), "bundle-passphrase", apply=True)
        result.put((True, bool(imported.get("committed"))))
    except Exception as exc:  # noqa: BLE001 - the worker reports any failure to the test
        result.put((False, type(exc).__name__))


def test_cookie_name_metadata_is_not_treated_as_plaintext_secret():
    assert _contains_secret_keys({"cookie_names": ["uid", "pass"]}) is False
    assert _contains_secret_keys({"telegram": {"token": "plaintext"}}) is True


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_import_rejects_non_finite_json_history_values(constant):
    raw = ('{"schema_version":1,"topics":{"topic-1":{"items":{"file":{"progress":' + constant + "}}}}}").encode()
    history = _parse_mapping(raw, label="download_history.json")
    with pytest.raises(ExportImportError, match="non-finite"):
        _validate_history_schema(history)


@pytest.mark.parametrize("constant", [".nan", ".inf", "-.inf"])
def test_import_rejects_non_finite_yaml_config_values(constant):
    config = _parse_mapping(f"extra: {constant}".encode(), label="config.yaml")
    with pytest.raises(ExportImportError, match="non-finite"):
        _validate_config_schema(config)


@pytest.mark.parametrize(("label", "prefix"), [("state.json", b'{"nested":'), ("config.yaml", b"nested: ")])
def test_import_rejects_extreme_nesting_without_recursion_escape(label, prefix):
    raw = prefix + b"[" * 1500 + b"0" + b"]" * 1500 + (b"}" if label == "state.json" else b"")
    with pytest.raises(ExportImportError, match=r"invalid|nested too deeply"):
        _validate_tree(_parse_mapping(raw, label=label), label=label)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_export_rejects_non_finite_numbers(value):
    with pytest.raises(ExportImportError, match="not JSON serializable"):
        _json_bytes({"nested": [value]})


def test_import_rejects_deep_envelope_and_manifest(tmp_path):
    deep = b"[" * 1500 + b"0" + b"]" * 1500
    bundle = tmp_path / "malformed.towx"
    bundle.write_bytes(b'{"nested":' + deep + b"}")
    with pytest.raises(ExportImportError, match="export bundle envelope"):
        _read_bundle(bundle, "passphrase")
    with pytest.raises(ExportImportError, match=r"manifest\.json|invalid bundle manifest"):
        _validated_payload(_zip_payload({"manifest.json": b'{"nested":' + deep + b"}"}))


def test_import_rejects_non_finite_optional_events():
    from tow import __version__
    from tow.bundle import FORMAT, _sha256

    members = {
        "config.yaml": b"{}",
        "state.json": b'{"topics":[],"mirrors":{}}',
        "download_history.json": b'{"schema_version":1,"topics":{}}',
        "secrets.json": b"{}",
        "events.json": b'{"events":[{"event_id":Infinity}]}',
    }
    members["manifest.json"] = json.dumps(
        {
            "format": FORMAT,
            "schema_version": 1,
            "source_version": __version__,
            "members": sorted(members),
            "sha256": {name: _sha256(data) for name, data in members.items()},
        }
    ).encode()
    with pytest.raises(ExportImportError, match=r"events\.json contains a non-finite number"):
        _validated_payload(_zip_payload(members))


@pytest.mark.parametrize(
    "metadata",
    [
        {"undo": {"secret_scope": ["telegram"], "secret_undo_ref": "r", "secrets_undo_ref": "settings-v1"}},
        {"clients": {"qb": {"secrets_ref": "clients.qb"}}},
        {"lan_auth": True, "telegram": {"token": ""}, "cookies_by_origin": None},
    ],
)
def test_tow_metadata_and_empty_slots_are_not_plaintext_secrets(metadata):
    assert _contains_secret_keys(metadata) is False


@pytest.mark.parametrize("leak", [{"qbit_password": "x"}, {"site": {"api_key": "x"}}, {"a": [{"cookies": {"k": "v"}}]}])
def test_real_secret_fields_are_still_refused(leak):
    assert _contains_secret_keys(leak) is True


@pytest.mark.parametrize(
    "bucket",
    [
        {"frozen": "false"},
        {"fail": "broken"},
        {"fail": {"https://host": -1}},
        {"cool": {"https://host": float("inf")}},
        {"active": 42},
    ],
)
def test_import_rejects_malformed_mirror_bucket(bucket):
    with pytest.raises(ExportImportError, match=r"mirror|non-finite"):
        _validate_state_schema({"topics": [], "mirrors": {"site": bucket}})


def test_atomic_import_write_does_not_overwrite_predictable_temp_collision(tmp_path):
    target = tmp_path / "state.json"
    predictable = tmp_path / "state.json.tow-import.tmp"
    predictable.write_bytes(b"unrelated sentinel")

    _atomic_write(target, b"new content")

    assert target.read_bytes() == b"new content"
    assert predictable.read_bytes() == b"unrelated sentinel"


def _seed_source(monkeypatch, root: Path) -> tuple[Path, dict, dict, dict]:
    config = {
        "bind": "127.0.0.1",
        "port": 8787,
        "interval_sec": 600,
        "save_roots": ["C:/old/media"],
        "trackers": {"example": {"url_regex": "example\\.test"}},
    }
    state = {
        "topics": [
            {
                "id": "topic-1",
                "title": "Example [01x01-02 из 2]",
                "hash": "ABC123",
                "save_path": "C:/old/media/Example",
                "client_id": "main",
            }
        ],
        "mirrors": {"example": {"preferred": "mirror-a"}},
    }
    history = {
        "schema_version": 1,
        "topics": {"topic-1": {"items": {"one": {"label": "S01E01", "completed": True}}}},
    }
    secrets = {"qbittorrent": {"host": "http://qbit.local", "username": "fixture-user", "password": "fixture-secret"}}
    monkeypatch.setenv("TOW_HOME", str(root / "data"))
    root.mkdir(parents=True, exist_ok=True)
    config_file = root / "config.yaml"
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("TOW_CONFIG", str(config_file))
    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    save_state(state)
    save_download_history(history)
    save_secrets(secrets)
    ref = save_secret_undo({"telegram": {"token": "undo-token"}})
    save_state({**state, "undo": {"kind": "settings", "secrets_undo_ref": ref}})
    return config_file, state, history, secrets


def test_export_bundle_is_encrypted_and_contains_authoritative_sections(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"

    result = export_bundle(bundle, "correct horse battery staple")

    assert result["format"] == "tow-export-v1"
    assert result["preview"] is False
    assert result["source_mutation"] is False
    assert bundle.is_file()
    raw = bundle.read_bytes()
    assert b"fixture-secret" not in raw
    assert b"password" not in raw
    assert b"undo-token" not in raw
    assert result["members"] == sorted(
        [
            "config.yaml",
            "state.json",
            "download_history.json",
            "secrets.json",
            "secrets_undo.json",
        ]
    )


def _exact_topic():
    return {
        "id": "manual-fixture",
        "selection": {
            "mode": "exact",
            "source_hash": "A" * 40,
            "files": [{"path": "Season 1/literal*.mkv", "size": 9007199254740993}],
        },
    }


@pytest.mark.parametrize("kind", ["active", "undo", "legacy"])
def test_every_export_preserves_schema_and_old_reader_refuses_before_writes(tmp_path, monkeypatch, kind):
    from tow import store

    root = tmp_path / "source"
    _seed_source(monkeypatch, root)
    state = {"topics": [], "mirrors": {}}
    if kind == "active":
        state["topics"] = [_exact_topic()]
    elif kind == "undo":
        state["undo"] = {"kind": "topic", "item": _exact_topic()}
    save_state(state)
    archive = tmp_path / "guarded.towx"
    export_bundle(archive, "bundle-passphrase")
    parsed = _read_bundle(archive, "bundle-passphrase")
    assert parsed["state"]["schema_version"] == store.STATE_SCHEMA_VERSION == 2
    targets = [config_path(), state_path(), store.download_history_path(), store.encrypted_secrets_path()]
    before = {path: path.read_bytes() for path in targets}
    monkeypatch.setattr(store, "STATE_SCHEMA_VERSION", 1)
    with pytest.raises(ExportImportError, match=r"state\.json"):
        import_bundle(archive, "bundle-passphrase", apply=True)
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not (root / "data" / "import-checkpoints").exists()


@pytest.mark.parametrize("schema", [None, 0, 1])
@pytest.mark.parametrize("kind", ["active", "undo"])
def test_unmarked_exact_selection_is_refused_even_inside_undo(schema, kind):
    state = {"topics": [], "mirrors": {}}
    if schema is not None:
        state["schema_version"] = schema
    if kind == "active":
        state["topics"] = [_exact_topic()]
    else:
        state["undo"] = {"kind": "topic", "item": _exact_topic()}
    with pytest.raises(ExportImportError, match="require schema 2"):
        _validate_state_schema(state)


def test_exact_selection_export_apply_and_readback_keep_format_guard(tmp_path, monkeypatch):
    from tow import store

    root = tmp_path / "source"
    _seed_source(monkeypatch, root)
    topic = _exact_topic()
    save_state({"topics": [topic], "mirrors": {}})
    archive = tmp_path / "exact.towx"
    export_bundle(archive, "bundle-passphrase")
    save_state({"topics": [], "mirrors": {}})
    result = import_bundle(archive, "bundle-passphrase", apply=True)
    assert result["committed"] is True
    assert json.loads(state_path().read_bytes())["schema_version"] == store.STATE_SCHEMA_VERSION == 2
    assert load_state()["topics"] == [topic]


def test_import_preview_is_no_write_and_apply_reencrypts_for_destination(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source_config, source_state, source_history, source_secrets = _seed_source(monkeypatch, source)
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")
    source_config_bytes = source_config.read_bytes()

    dest = tmp_path / "destination"
    monkeypatch.setenv("TOW_HOME", str(dest / "data"))
    dest_config = dest / "config.yaml"
    monkeypatch.setenv("TOW_CONFIG", str(dest_config))
    monkeypatch.setenv("TOW_MASTER_KEY", _key(b"destination-key-material-32bytes"))

    preview = import_bundle(bundle, "bundle-passphrase", path_maps=["C:/old/media=D:/new/media"])

    assert preview["preview"] is True
    assert preview["apply_required"] is True
    assert not dest_config.exists()
    assert not state_path().exists()

    applied = import_bundle(bundle, "bundle-passphrase", apply=True, path_maps=["C:/old/media=D:/new/media"])

    assert applied["preview"] is False
    assert applied["checkpoint"]
    assert yaml.safe_load(dest_config.read_text(encoding="utf-8")) == yaml.safe_load(source_config_bytes)
    imported_state = json.loads(state_path().read_text(encoding="utf-8"))
    assert imported_state["topics"][0]["save_path"] == "D:/new/media/Example"
    assert imported_state["topics"][0]["id"] == source_state["topics"][0]["id"]
    assert json.loads((dest / "data" / "download_history.json").read_text(encoding="utf-8")) == source_history
    assert load_secrets() == source_secrets
    assert load_secret_undo("settings-v1") == {"telegram": {"token": "undo-token"}}
    assert (dest / "data" / "secrets.enc").read_bytes() != (source / "data" / "secrets.enc").read_bytes()


def test_wrong_passphrase_and_tampered_bundle_do_not_write_destination(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")
    dest = tmp_path / "destination"
    monkeypatch.setenv("TOW_HOME", str(dest / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(dest / "config.yaml"))
    sentinel = dest / "data" / "state.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text('{"sentinel":true}\n', encoding="utf-8")
    before = sentinel.read_bytes()

    with pytest.raises(ExportImportError, match="passphrase"):
        import_bundle(bundle, "wrong-passphrase", apply=True)
    assert sentinel.read_bytes() == before

    outer = json.loads(bundle.read_text(encoding="utf-8"))
    token = outer["payload"]
    outer["payload"] = token[:-1] + ("A" if token[-1] != "A" else "B")
    bundle.write_text(json.dumps(outer), encoding="utf-8")
    with pytest.raises(ExportImportError, match="decrypt"):
        import_bundle(bundle, "bundle-passphrase", apply=True)
    assert sentinel.read_bytes() == before


def test_traversal_member_is_rejected_without_write(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")
    outer = json.loads(bundle.read_text(encoding="utf-8"))
    from tow import bundle as tow_bundle

    payload = tow_bundle._decrypt_outer(outer, "bundle-passphrase")
    with zipfile.ZipFile(io.BytesIO(payload), "r") as source_zip:
        members = {name: source_zip.read(name) for name in source_zip.namelist()}
    members["../state.json"] = members["state.json"]
    manifest = json.loads(members["manifest.json"])
    manifest["members"].append("../state.json")
    members["manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()
    malicious_payload = tow_bundle._zip_payload(members)
    outer["payload"] = tow_bundle._encrypt_outer(
        malicious_payload, "bundle-passphrase", base64.urlsafe_b64decode(outer["kdf"]["salt"])
    )
    bundle.write_text(json.dumps(outer), encoding="utf-8")

    monkeypatch.setenv("TOW_HOME", str(tmp_path / "destination" / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(tmp_path / "destination" / "config.yaml"))
    with pytest.raises(ExportImportError, match=r"relative|allowlist|member"):
        import_bundle(bundle, "bundle-passphrase", apply=True)
    assert not config_path().exists()


def test_import_rollback_restores_exact_destination_files(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")

    dest = tmp_path / "destination"
    monkeypatch.setenv("TOW_HOME", str(dest / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(dest / "config.yaml"))
    monkeypatch.setenv("TOW_MASTER_KEY", _key(b"destination-key-material-32bytes"))
    dest_config = dest / "config.yaml"
    dest_config.parent.mkdir(parents=True)
    dest_config.write_text("bind: 127.0.0.1\nport: 9999\n", encoding="utf-8")
    save_state({"topics": [{"id": "old"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {"old": {}}})
    save_secrets({"qbittorrent": {"host": "http://old", "password": "old-secret"}})
    before = {
        "config": dest_config.read_bytes(),
        "state": state_path().read_bytes(),
        "history": (dest / "data" / "download_history.json").read_bytes(),
        "secrets": (dest / "data" / "secrets.enc").read_bytes(),
    }

    applied = import_bundle(bundle, "bundle-passphrase", apply=True)
    rollback = rollback_import(Path(applied["checkpoint"]), apply=True)

    assert rollback["ok"] is True
    assert dest_config.read_bytes() == before["config"]
    assert state_path().read_bytes() == before["state"]
    assert (dest / "data" / "download_history.json").read_bytes() == before["history"]
    assert (dest / "data" / "secrets.enc").read_bytes() == before["secrets"]
    assert load_secrets()["qbittorrent"]["password"] == "old-secret"


def test_import_log_failure_keeps_committed_result_and_readable_state(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")
    dest = tmp_path / "destination"
    monkeypatch.setenv("TOW_HOME", str(dest / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(dest / "config.yaml"))
    monkeypatch.setenv("TOW_MASTER_KEY", _key(b"destination-key-material-32bytes"))

    def fail_log(*args, **kwargs):
        raise OSError("log unavailable")

    monkeypatch.setattr("tow.bundle.log_event", fail_log)
    result = import_bundle(bundle, "bundle-passphrase", apply=True)

    assert result["committed"] is True
    assert result["read_back"] is True
    assert result["log_recorded"] is False
    assert config_path().is_file()
    assert state_path().is_file()


def test_import_reports_a_real_best_effort_log_write_failure(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")

    def unavailable_log():
        raise OSError("synthetic audit storage unavailable")

    monkeypatch.setattr("tow.log.log_path", unavailable_log)
    result = import_bundle(bundle, "bundle-passphrase", apply=True)

    assert result["committed"] is True
    assert result["read_back"] is True
    assert result["log_recorded"] is False
    assert result["log_error"]
    marker = Path(result["checkpoint"]) / "TRANSACTION.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["log_recorded"] is False


def test_incomplete_import_transaction_is_recovered_before_next_import(tmp_path, monkeypatch):
    from tow import bundle as tow_bundle

    dest = tmp_path / "destination"
    config_file, _, _, _ = _seed_source(monkeypatch, dest)
    checkpoint = tow_bundle._create_import_checkpoint()
    original_config = config_file.read_bytes()
    original_state = json.loads(state_path().read_text(encoding="utf-8"))
    original_history = json.loads((dest / "data" / "download_history.json").read_text(encoding="utf-8"))
    transaction = checkpoint / "TRANSACTION.json"
    assert json.loads(transaction.read_text(encoding="utf-8"))["status"] == "prepared"

    # What a process killed mid-import leaves behind: raw half-written targets, no lock held.
    config_file.write_text("bind: 0.0.0.0\nport: 1\n", encoding="utf-8")
    state_path().write_text(json.dumps({"topics": [], "mirrors": {}}), encoding="utf-8")
    (dest / "data" / "download_history.json").write_text('{"schema_version": 1, "topics": {}}', encoding="utf-8")

    # Taking the lock already runs the recovery hook, so the explicit call may find nothing left.
    tow_bundle.recover_import_transactions()

    assert config_file.read_bytes() == original_config
    assert json.loads(state_path().read_text(encoding="utf-8")) == original_state
    assert json.loads((dest / "data" / "download_history.json").read_text(encoding="utf-8")) == original_history
    assert json.loads(transaction.read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_crashed_import_is_rolled_back_before_any_other_writer(tmp_path, monkeypatch):
    # N3: not only the next import - the next writer of any kind (check, web edit) recovers first.
    from tow import bundle as tow_bundle

    dest = tmp_path / "destination"
    config_file, _, _, _ = _seed_source(monkeypatch, dest)
    tow_bundle._create_import_checkpoint()
    original_config = config_file.read_bytes()
    original_state = json.loads(state_path().read_text(encoding="utf-8"))
    config_file.write_text("bind: 0.0.0.0\nport: 1\n", encoding="utf-8")
    state_path().write_text(json.dumps({"topics": [], "mirrors": {}}), encoding="utf-8")

    with persistence_lock():
        assert config_file.read_bytes() == original_config
        assert json.loads(state_path().read_text(encoding="utf-8")) == original_state


def test_unreadable_import_journal_refuses_other_writes_until_repaired(tmp_path, monkeypatch):
    from tow import bundle as tow_bundle

    dest = tmp_path / "destination"
    _seed_source(monkeypatch, dest)
    checkpoint = tow_bundle._create_import_checkpoint()
    transaction = checkpoint / "TRANSACTION.json"
    original = transaction.read_bytes()
    transaction.write_text("{broken", encoding="utf-8")
    before = state_path().read_bytes()

    with pytest.raises(ExportImportError):
        save_state({"topics": [{"id": "must-not-overwrite"}], "mirrors": {}})

    assert state_path().read_bytes() == before
    assert transaction.read_text(encoding="utf-8") == "{broken"
    with pytest.raises(ExportImportError):
        tow_bundle.recover_import_transactions()
    transaction.write_bytes(original)
    tow_bundle.recover_import_transactions()
    save_state({"topics": [{"id": "writable-after-recovery"}], "mirrors": {}})
    assert load_state()["topics"] == [{"id": "writable-after-recovery"}]


def test_import_rollback_preview_rejects_missing_backup(tmp_path, monkeypatch):
    from tow import bundle as tow_bundle

    _seed_source(monkeypatch, tmp_path / "destination")
    checkpoint = tow_bundle._create_import_checkpoint()
    # A finished import: a still-"prepared" one would be recovered (and fail closed) on lock.
    tow_bundle._write_import_transaction(checkpoint, status="committed")
    (checkpoint / "files" / "config.yaml").unlink()

    with pytest.raises(ExportImportError, match=r"backup|checkpoint"):
        rollback_import(checkpoint, apply=False)


def test_import_rejects_malformed_nested_state_before_path_mapping(tmp_path, monkeypatch):
    from tow import bundle as tow_bundle

    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")
    outer = json.loads(bundle.read_text(encoding="utf-8"))
    members = tow_bundle._unzip_payload(tow_bundle._decrypt_outer(outer, "bundle-passphrase"))
    state_data = json.loads(members["state.json"])
    state_data["topics"] = [1]
    members["state.json"] = tow_bundle._json_bytes(state_data)
    manifest = json.loads(members["manifest.json"])
    manifest["sha256"]["state.json"] = tow_bundle._sha256(members["state.json"])
    members["manifest.json"] = tow_bundle._json_bytes(manifest)
    payload = tow_bundle._zip_payload(members)
    salt = base64.urlsafe_b64decode(outer["kdf"]["salt"])
    outer["payload"] = tow_bundle._encrypt_outer(payload, "bundle-passphrase", salt)
    bundle.write_text(json.dumps(outer), encoding="utf-8")

    with pytest.raises(ExportImportError, match=r"state|topic|schema"):
        import_bundle(bundle, "bundle-passphrase")


def test_optional_log_member_is_redacted_inside_encrypted_bundle(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    log_event("test", token="fixture-token", detail="safe-detail")
    bundle = tmp_path / "tow-with-log.towx"

    result = export_bundle(bundle, "bundle-passphrase", include_log=True)

    assert "events.json" in result["members"]
    from tow import bundle as tow_bundle

    outer = json.loads(bundle.read_text(encoding="utf-8"))
    members = tow_bundle._unzip_payload(tow_bundle._decrypt_outer(outer, "bundle-passphrase"))
    events = json.loads(members["events.json"])
    assert all("token" not in event for event in events["events"])
    assert all("detail" not in event for event in events["events"])
    assert b"fixture-token" not in bundle.read_bytes()
    assert b"safe-detail" not in bundle.read_bytes()


def test_cli_export_and_import_preview_use_interactive_passphrase(tmp_path, monkeypatch, capsys):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "cli.towx"
    answers = iter(["cli-passphrase", "cli-passphrase"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))

    from tow.cli import main

    assert main(["export", "--output", str(bundle), "--json"]) == 0
    export_output = json.loads(capsys.readouterr().out)
    assert export_output["ok"] is True
    assert export_output["source_master_key"] == "not-included"

    monkeypatch.setenv("TOW_HOME", str(tmp_path / "destination" / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(tmp_path / "destination" / "config.yaml"))
    answers = iter(["cli-passphrase"])
    assert main(["import", "--input", str(bundle), "--json"]) == 0
    import_output = json.loads(capsys.readouterr().out)
    assert import_output["preview"] is True
    assert not (tmp_path / "destination" / "config.yaml").exists()


def test_import_apply_requires_destination_key_before_any_write(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "destination" / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(tmp_path / "destination" / "config.yaml"))
    monkeypatch.delenv("TOW_MASTER_KEY", raising=False)
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)

    with pytest.raises(ExportImportError, match="destination master key"):
        import_bundle(bundle, "bundle-passphrase", apply=True)
    assert not config_path().exists()


def test_concurrent_import_waits_for_shared_persistence_lock(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")

    destination = tmp_path / "destination"
    destination_home = destination / "data"
    destination_config = destination / "config.yaml"
    destination_key = _key(b"destination-key-material-32bytes")
    monkeypatch.setenv("TOW_HOME", str(destination_home))
    monkeypatch.setenv("TOW_CONFIG", str(destination_config))
    monkeypatch.setenv("TOW_MASTER_KEY", destination_key)
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    result = context.Queue()

    process = context.Process(
        target=_import_worker,
        args=(
            str(bundle),
            str(destination_home),
            str(destination_config),
            destination_key,
            started,
            result,
        ),
    )
    with reaped(process):
        with persistence_lock():
            process.start()
            assert started.wait(10)
            process.join(0.5)
            assert process.is_alive()

        process.join(20)
        assert process.exitcode == 0
        assert result.get(timeout=5) == (True, True)
    assert destination_config.is_file()


def test_import_write_failure_automatically_restores_checkpoint(tmp_path, monkeypatch):
    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")

    dest = tmp_path / "destination"
    monkeypatch.setenv("TOW_HOME", str(dest / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(dest / "config.yaml"))
    monkeypatch.setenv("TOW_MASTER_KEY", _key(b"destination-key-material-32bytes"))
    dest_config = dest / "config.yaml"
    dest_config.parent.mkdir(parents=True)
    dest_config.write_text("bind: 127.0.0.1\nport: 9999\n", encoding="utf-8")
    save_state({"topics": [{"id": "old"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {"old": {}}})
    save_secrets({"qbittorrent": {"host": "http://old", "password": "old-secret"}})

    def fail_save(_data):
        from tow.store import SecretStoreError

        raise SecretStoreError("fixture write failure")

    monkeypatch.setattr("tow.bundle.save_secrets", fail_save)
    with pytest.raises(ExportImportError, match="destination was restored"):
        import_bundle(bundle, "bundle-passphrase", apply=True)


def test_import_rollback_compensates_after_late_restore_failure(tmp_path, monkeypatch):
    from tow import bundle as tow_bundle

    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")
    dest = tmp_path / "destination"
    monkeypatch.setenv("TOW_HOME", str(dest / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(dest / "config.yaml"))
    monkeypatch.setenv("TOW_MASTER_KEY", _key(b"destination-key-material-32bytes"))
    dest_config = dest / "config.yaml"
    dest_config.parent.mkdir(parents=True)
    dest_config.write_text("bind: 127.0.0.1\nport: 9999\n", encoding="utf-8")
    save_state({"topics": [{"id": "old"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {"old": {}}})
    save_secrets({"qbittorrent": {"host": "http://old", "password": "old-secret"}})

    applied = import_bundle(bundle, "bundle-passphrase", apply=True)
    targets = {
        "config": dest_config,
        "state": state_path(),
        "history": dest / "data" / "download_history.json",
        "secrets": dest / "data" / "secrets.enc",
    }
    after_import = {name: path.read_bytes() for name, path in targets.items()}
    original_atomic = tow_bundle._atomic_write
    calls = 0

    def fail_once(path, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated late rollback failure")
        return original_atomic(path, content)

    monkeypatch.setattr(tow_bundle, "_atomic_write", fail_once)
    with pytest.raises(ExportImportError, match="rollback failed; destination preserved"):
        tow_bundle.rollback_import(Path(applied["checkpoint"]), apply=True)

    assert {name: path.read_bytes() for name, path in targets.items()} == after_import
    transaction = json.loads((Path(applied["checkpoint"]) / "TRANSACTION.json").read_text(encoding="utf-8"))
    assert transaction["status"] == "committed"
    assert transaction["rollback_status"] == "failed_compensated"


def test_login_form_is_valid_non_secret_config_metadata():
    from tow.bundle import (
        ExportImportError,
        _contains_secret_keys,
        _validate_config_schema,
    )

    config = {
        "trackers": {
            "rutracker": {
                "login_form": {"user_field": "login_username", "pw_field": "login_password", "extra": {"login": "Вход"}}
            }
        }
    }
    _validate_config_schema(config)
    assert not _contains_secret_keys(config)
    with pytest.raises(ExportImportError, match="login_form"):
        _validate_config_schema({"trackers": {"x": {"login_form": {"password": "leak"}}}})


def test_production_export_kdf_cost_is_not_lowered():
    # Tests run with 100k iterations (conftest); the shipped value must stay at 600k.
    import re

    from tow import bundle as tow_bundle

    source = Path(tow_bundle.__file__).read_text(encoding="utf-8")
    assert re.search(r"^KDF_ITERATIONS = 600_000$", source, flags=re.MULTILINE)


def test_a_failed_second_rollback_does_not_undo_later_edits(tmp_path, monkeypatch):
    # Found while raising coverage: the compensation wrote "prepared" over a
    # "rolled_back" marker, so the recovery hook rolled back again at the next lock
    # and silently threw away every edit made after the first rollback.
    from tow import bundle as tow_bundle

    _seed_source(monkeypatch, tmp_path / "source")
    bundle = tmp_path / "tow.towx"
    export_bundle(bundle, "bundle-passphrase")
    dest = tmp_path / "destination"
    monkeypatch.setenv("TOW_HOME", str(dest / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(dest / "config.yaml"))
    monkeypatch.setenv("TOW_MASTER_KEY", _key(b"destination-key-material-32bytes"))
    (dest / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
    (dest / "config.yaml").write_text("bind: 127.0.0.1\nport: 8787\ntrackers: {}\n", encoding="utf-8")
    save_state({"topics": [{"id": "destination-before"}], "mirrors": {}})  # the checkpoint holds content
    checkpoint = Path(import_bundle(bundle, "bundle-passphrase", apply=True)["checkpoint"])
    rollback_import(checkpoint, apply=True)
    save_state({"topics": [{"id": "edited-after-rollback"}], "mirrors": {}})

    real_write = tow_bundle._atomic_write
    calls = []

    def second_write_fails(path, content):
        calls.append(path)
        if Path(path).name == "state.json" and not calls[:-1].count(path):  # once: the rollback write
            raise OSError("disk hiccup")
        return real_write(path, content)

    monkeypatch.setattr(tow_bundle, "_atomic_write", second_write_fails)
    with pytest.raises(ExportImportError, match="destination preserved"):
        rollback_import(checkpoint, apply=True)
    monkeypatch.setattr(tow_bundle, "_atomic_write", real_write)

    with persistence_lock():  # the recovery hook runs here
        pass

    assert load_state()["topics"] == [{"id": "edited-after-rollback"}]
