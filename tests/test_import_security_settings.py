"""A transfer file or a restore never changes this computer's security settings by itself.

Someone may hand the owner "my setup" as a ``.towx`` with its passphrase: ``tow import --apply``
took its network access (bind 0.0.0.0, allow_lan), its password, its backup folders and its
opt-ins for the home network and shares as they were. A restore from a signed-in device on the
network brought back an access provider or a private-host opt-in the owner had since turned off.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml

from tow.auth import lan_password_record
from tow.bundle import export_bundle
from tow.config import load_config, save_config
from tow.store import load_secrets, load_state, save_download_history, save_secrets, save_state

PROVIDERS = {
    "enabled": True,
    "providers": [{"id": "local-browser", "kind": "flaresolverr", "url": "http://127.0.0.1:8191/v1", "enabled": True}],
}
OPENED = {
    "bind": "0.0.0.0",
    "allow_lan": True,
    "backup_dir": "D:/handed/night",
    "allow_unc_save_paths": True,
    "allow_private_tracker_hosts": True,
    "allow_private_notifier_hosts": True,
    "protection": PROVIDERS,
}


def _key(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed).decode()


def _install(monkeypatch, root: Path, config: dict, key: bytes) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    config_file = root / "config.yaml"
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("TOW_HOME", str(root / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(config_file))
    monkeypatch.setenv("TOW_MASTER_KEY", _key(key))
    return config_file


@pytest.fixture
def handed_file(tmp_path, monkeypatch):
    """A file from someone else's TOW that is open to the network with their password, and a
    local-only destination (this computer) without a password."""
    base = {"port": 8787, "interval_sec": 600, "trackers": {}, "language": "en"}
    _install(monkeypatch, tmp_path / "source", {**base, **OPENED}, b"source-key-material-for-tests-32")
    save_state({"topics": [{"id": "theirs", "title": "Show", "save_path": "D:/media/Show"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({"lan_auth": lan_password_record("their-password-123")})
    bundle = tmp_path / "setup.towx"
    export_bundle(bundle, "bundle-passphrase")
    dest = _install(
        monkeypatch,
        tmp_path / "destination",
        {**base, "bind": "127.0.0.1", "allow_lan": False},
        b"destination-key-material-32bytes",
    )
    save_state({"topics": [{"id": "mine"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({"telegram": {"token": "mine"}})
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "bundle-passphrase")
    return bundle, dest


def _cli(capsys, *args: str) -> dict:
    from tow.cli import main

    assert main(["import", *args, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_the_preview_names_every_security_setting_the_file_would_change(handed_file, capsys):
    bundle, _dest = handed_file
    preview = _cli(capsys, "--input", str(bundle))
    changes = {item["setting"]: item for item in preview["security_settings"]}
    assert set(changes) == {*OPENED, "lan_auth"}
    assert changes["allow_lan"]["file"] is True
    assert changes["allow_lan"]["this_computer"] is False
    assert changes["bind"]["file"] == "0.0.0.0"
    assert changes["lan_auth"] == {"setting": "lan_auth", "file": "set", "this_computer": None, "kept": True}
    assert all(item["kept"] for item in changes.values())


def test_import_apply_keeps_this_computers_security_settings(handed_file, capsys):
    bundle, dest = handed_file
    applied = _cli(capsys, "--input", str(bundle), "--apply")
    assert applied["committed"] is True
    config = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert config["bind"] == "127.0.0.1"
    assert config["allow_lan"] is False
    for key in ("backup_dir", "allow_unc_save_paths", "allow_private_tracker_hosts", "allow_private_notifier_hosts"):
        assert key not in config, key
    assert "protection" not in config
    assert "lan_auth" not in load_secrets()
    assert [topic["id"] for topic in load_state()["topics"]] == ["theirs"]  # the data itself is imported


def test_the_file_s_security_settings_need_an_explicit_flag(handed_file, capsys):
    bundle, dest = handed_file
    applied = _cli(capsys, "--input", str(bundle), "--apply", "--accept-security-settings")
    assert not any(item["kept"] for item in applied["security_settings"])
    config = yaml.safe_load(dest.read_text(encoding="utf-8"))
    for key, value in OPENED.items():
        assert config[key] == value, key
    assert "lan_auth" in load_secrets()


def test_the_text_preview_says_what_stays_and_how_to_take_the_file_s(handed_file, capsys):
    from tow.cli import main
    from tow.i18n import t

    bundle, _dest = handed_file
    assert main(["import", "--input", str(bundle)]) == 0
    text = capsys.readouterr().out
    assert t("cli.bundle.security_header") in text
    assert t("cli.bundle.security_kept") in text
    assert "allow_private_tracker_hosts" in text


def _damage_this_computers_secrets() -> None:
    from tow.store import encrypted_secrets_path

    save_secrets({"lan_auth": lan_password_record("my-own-password-1")})
    encrypted_secrets_path().write_bytes(b"not an encrypted store")


def test_a_good_file_is_imported_when_this_computers_passwords_cannot_be_opened(handed_file, capsys):
    """Restoring a file is the way out of a lost master key or a damaged store: it is not refused
    as "damaged", and the file's password for other devices is not taken instead of this one."""
    from tow.cli import main
    from tow.i18n import t

    bundle, dest = handed_file
    _damage_this_computers_secrets()

    preview = _cli(capsys, "--input", str(bundle))
    assert preview["local_secrets_unreadable"] is True
    changes = {item["setting"]: item for item in preview["security_settings"]}
    assert changes["lan_auth"] == {"setting": "lan_auth", "file": "set", "this_computer": "unknown", "kept": True}
    assert main(["import", "--input", str(bundle)]) == 0
    assert t("cli.bundle.local_secrets_unreadable") in capsys.readouterr().out

    applied = _cli(capsys, "--input", str(bundle), "--apply")
    assert applied["committed"] is True
    assert "lan_auth" not in load_secrets()
    assert yaml.safe_load(dest.read_text(encoding="utf-8"))["allow_lan"] is False
    assert [topic["id"] for topic in load_state()["topics"]] == ["theirs"]


def test_a_restore_from_the_browser_with_unopenable_passwords_does_not_call_the_file_damaged(tmp_path, monkeypatch):
    """The web restore first saves the current data as a restore point; with passwords that cannot
    be opened it cannot, and says that (the file is fine), not that the file is damaged."""
    from tow.restore_points import (
        CREATE_FAILED,
        RestorePointError,
        check_portable_bundle,
        export_portable_bundle,
        restore_portable_bundle,
    )

    base = {"bind": "127.0.0.1", "port": 8787, "allow_lan": False, "trackers": {}, "language": "en"}
    _install(monkeypatch, tmp_path / "install", base, b"y" * 32)
    save_state({"topics": [{"id": "before"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    file = tmp_path / "mine.towx"
    export_portable_bundle(file)
    _damage_this_computers_secrets()

    check_portable_bundle(file)
    with pytest.raises(RestorePointError) as refused:
        restore_portable_bundle(file)
    assert refused.value.kind == CREATE_FAILED


def test_a_restore_keeps_this_computers_opt_ins_and_access_providers(tmp_path, monkeypatch):
    """A restore point made while an access provider and the private-host opt-ins were on, then
    restored (also from a device on the network) after the owner turned them off here."""
    from tow.restore_points import create_restore_point, restore_from_point

    base = {"bind": "127.0.0.1", "port": 8787, "allow_lan": False, "trackers": {}, "language": "en"}
    _install(
        monkeypatch, tmp_path / "install", {**base, **{k: v for k, v in OPENED.items() if k not in base}}, b"x" * 32
    )
    save_state({"topics": [{"id": "before"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    point = create_restore_point()
    live = load_config()
    for key in ("allow_unc_save_paths", "allow_private_tracker_hosts", "allow_private_notifier_hosts", "protection"):
        live.pop(key, None)
    save_config(live)

    restore_from_point(point["id"])

    restored = load_config()
    assert [topic["id"] for topic in load_state()["topics"]] == ["before"]
    for key in ("allow_unc_save_paths", "allow_private_tracker_hosts", "allow_private_notifier_hosts"):
        assert not restored.get(key), key
    assert not (restored.get("protection") or {}).get("providers")
