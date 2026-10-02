"""The master key inside the install: keys/master.key, `tow keys adopt`, and it never leaves.

Order: TOW_MASTER_KEY (tests, CI) > TOW_MASTER_KEY_FILE (explicit, older installs) >
<root>/keys/master.key. The key is never printed and never part of a copy of the data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from tow import cli
from tow.i18n import t
from tow.paths import key_file, keys_dir
from tow.store import (
    MasterKeyError,
    SecretStoreError,
    adopt_master_key,
    load_secrets,
    master_key_file,
    save_secret_undo,
    save_secrets,
    secret_store_status,
)

SECRETS = {"telegram": {"token": "fixture-token"}}


@pytest.fixture
def no_env_key(monkeypatch):
    monkeypatch.delenv("TOW_MASTER_KEY", raising=False)
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)


def _key_at(path: Path) -> bytes:
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key + b"\n")
    return key


def _cli(args: list[str], capsys) -> tuple[int, dict]:
    code = cli.main([*args, "--json"])
    return code, json.loads(capsys.readouterr().out)


# --- where the key comes from -----------------------------------------------------------------


def test_the_install_key_is_used_when_nothing_else_is_given(no_env_key, tmp_path):
    _key_at(tmp_path / "keys" / "master.key")
    assert master_key_file() == key_file() == tmp_path / "keys" / "master.key"
    assert secret_store_status()["key_source"] == "install"
    save_secrets(SECRETS)
    assert load_secrets() == SECRETS


def test_an_explicit_key_file_wins_over_the_install_key(no_env_key, monkeypatch, tmp_path):
    _key_at(tmp_path / "keys" / "master.key")
    explicit = tmp_path / "elsewhere" / "master.key"
    _key_at(explicit)
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", str(explicit))
    assert master_key_file() == explicit
    assert secret_store_status()["key_source"] == "file"
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", "relative.key")  # inside the data folder, as before
    assert master_key_file() == tmp_path / "relative.key"


def test_the_environment_key_wins_over_every_file(monkeypatch, tmp_path):
    _key_at(tmp_path / "keys" / "master.key")
    assert master_key_file() is None  # conftest sets TOW_MASTER_KEY
    assert secret_store_status()["key_source"] == "env"


def test_a_key_left_in_data_by_an_older_install_is_found_without_a_launcher(no_env_key, monkeypatch, tmp_path, capsys):
    """data/master.key counts after TOW_MASTER_KEY_FILE and keys/master.key, on every start
    (before 1.21 only tow-env.cmd found it: scripts/tow and autostart did not)."""
    from tow.paths import data_dir

    legacy = data_dir() / "master.key"
    key = _key_at(legacy)
    assert master_key_file() == legacy
    assert secret_store_status()["key_source"] == "legacy"
    save_secrets(SECRETS)
    assert load_secrets() == SECRETS

    code, out = _cli(["keys", "adopt"], capsys)  # moved into keys/ like a TOW_MASTER_KEY_FILE key
    assert code == 0
    assert key.decode() not in json.dumps(out)
    assert key_file().read_bytes().strip() == key
    assert secret_store_status()["key_source"] == "install"  # keys/ wins from now on
    other = tmp_path / "explicit.key"
    _key_at(other)
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", str(other))
    assert master_key_file() == other  # the variable still wins over both


def test_no_key_anywhere_fails_closed_and_names_every_place(no_env_key):
    assert secret_store_status()["key_source"] == "missing"
    with pytest.raises(SecretStoreError, match=r"keys/master.key.*TOW_MASTER_KEY_FILE.*tow keys adopt"):
        save_secrets(SECRETS)


# --- tow secrets generate-key -----------------------------------------------------------------


def test_generate_key_defaults_to_the_install_keys_folder(no_env_key, tmp_path, capsys):
    code, out = _cli(["secrets", "generate-key"], capsys)
    assert code == 0
    assert out["key_file_created"] is True
    assert (tmp_path / "keys" / "master.key").is_file()
    assert out["message"] == t("cli.keys.generated", path=tmp_path / "keys" / "master.key")
    key = (tmp_path / "keys" / "master.key").read_bytes().strip()
    assert key.decode() not in json.dumps(out)
    save_secrets(SECRETS)  # the new key is in effect at once
    assert load_secrets() == SECRETS


def test_generate_key_never_replaces_or_competes_with_the_key_in_use(no_env_key, monkeypatch, tmp_path, capsys):
    explicit = tmp_path / "old" / "master.key"
    _key_at(explicit)
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", str(explicit))
    save_secrets(SECRETS)
    code, out = _cli(["secrets", "generate-key"], capsys)
    assert code == 3
    assert out["error"] == t("cli.keys.error.secrets_exist")
    assert not key_file().exists()

    code, out = _cli(["secrets", "generate-key", "--key-file", str(explicit)], capsys)
    assert code == 3
    assert out["error"] == t("cli.keys.error.exists")


def test_a_new_key_file_is_private_from_the_moment_it_exists(no_env_key, monkeypatch, tmp_path):
    """Created with O_EXCL and mode 0600 in one call - never with the umask's mode first."""
    import os
    import stat

    from tow import store

    opened: list[tuple[int, int]] = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *args, **kwargs):
        if str(path).endswith("master.key"):
            opened.append((flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", recording_open)
    path = store.generate_master_key()

    assert opened, "the key file is created through os.open"
    flags, mode = opened[0]
    assert flags & os.O_CREAT
    assert flags & os.O_EXCL
    assert mode == 0o600
    if os.name != "nt":  # Windows keeps the install folder's permissions
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(MasterKeyError) as again:
        store.generate_master_key(path)
    assert again.value.kind == "exists"


# --- tow keys adopt ---------------------------------------------------------------------------


@pytest.fixture
def legacy_key(no_env_key, monkeypatch, tmp_path) -> bytes:
    """An older install: the key outside the folder, named by TOW_MASTER_KEY_FILE."""
    outside = tmp_path / "LocalAppData" / "tow" / "master.key"
    key = _key_at(outside)
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", str(outside))
    save_secrets(SECRETS)
    save_secret_undo({"telegram": {"token": "older"}})
    return key


def test_adopt_copies_the_key_in_use_after_it_opens_the_secrets(legacy_key, monkeypatch, capsys):
    code, out = _cli(["keys", "adopt"], capsys)

    assert code == 0
    assert out["secrets_checked"] == 2  # secrets.enc and the settings undo
    assert out["already"] is False
    assert key_file().read_bytes().strip() == legacy_key
    assert legacy_key.decode() not in json.dumps(out)
    assert out["message"] == t("cli.keys.adopted", path=key_file(), count=2)
    monkeypatch.delenv("TOW_MASTER_KEY_FILE")  # the variable can go now
    assert secret_store_status()["key_source"] == "install"
    assert load_secrets() == SECRETS


def test_adopt_twice_is_harmless(legacy_key, capsys):
    assert adopt_master_key()["already"] is False
    code, out = _cli(["keys", "adopt"], capsys)
    assert code == 0
    assert out["already"] is True


def test_adopt_refuses_a_key_that_does_not_open_the_secrets(legacy_key, tmp_path, capsys):
    other = tmp_path / "other.key"
    _key_at(other)
    code, out = _cli(["keys", "adopt", "--from", str(other)], capsys)
    assert code == 3
    assert out["error"] == t("cli.keys.error.wrong_key")
    assert not key_file().exists()


def test_adopt_never_replaces_a_different_key_already_inside(legacy_key):
    planted = _key_at(key_file())
    with pytest.raises(MasterKeyError) as caught:
        adopt_master_key()
    assert caught.value.kind == "different"
    assert key_file().read_bytes().strip() == planted


@pytest.mark.parametrize(
    ("content", "kind"),
    [(None, "unreadable"), (b"", "unreadable"), (b"not-a-fernet-key", "invalid")],
)
def test_adopt_refuses_a_missing_empty_or_foreign_file(legacy_key, tmp_path, content, kind):
    source = tmp_path / "candidate.key"
    if content is not None:
        source.write_bytes(content)
    with pytest.raises(MasterKeyError) as caught:
        adopt_master_key(source)
    assert caught.value.kind == kind
    assert not key_file().exists()


def test_adopt_without_a_source_says_which_key_is_meant(no_env_key):
    with pytest.raises(MasterKeyError) as caught:
        adopt_master_key()
    assert caught.value.kind == "no_source"


def test_keys_status_names_the_files_not_the_key(legacy_key, capsys):
    code, out = _cli(["keys", "status"], capsys)
    assert code == 0
    assert out["key_source"] == "file"
    assert out["install_key_file"] == str(key_file())
    assert out["install_key_exists"] is False
    assert legacy_key.decode() not in json.dumps(out)


# --- the key never leaves the install ---------------------------------------------------------


def test_copies_of_the_data_never_carry_the_install_key(no_env_key, monkeypatch, tmp_path):
    from tow.bundle import _read_bundle, export_bundle
    from tow.restore_points import _passphrase, create_restore_point, restore_points_dir
    from tow.snapshots import create_snapshot

    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))  # the install layout: night copies outside data/
    key = _key_at(key_file())
    save_secrets(SECRETS)

    night = Path(create_snapshot()["snapshot"])
    assert keys_dir() not in night.parents
    for path in night.rglob("*"):
        if path.is_file():
            assert key not in path.read_bytes(), path
            assert path.name != "master.key"

    passphrase = "a-long-export-passphrase"
    export_bundle(tmp_path / "out.towx", passphrase)
    point = create_restore_point()
    for bundle, secret in ((tmp_path / "out.towx", passphrase), (restore_points_dir() / f"{point['id']}.towx", None)):
        members = _read_bundle(bundle, secret or _passphrase())["members"]
        assert not any("key" in name for name in members)
        assert all(key not in content for content in members.values())


def test_the_keys_folder_is_never_committed():
    ignored = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "keys/" in ignored
