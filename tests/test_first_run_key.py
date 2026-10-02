"""First run: TOW makes its own master key, says once to back it up; a missing key says how to fix it."""

from __future__ import annotations

import json

import pytest

from tow import cli
from tow.paths import data_dir, key_file


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("TOW_MASTER_KEY", raising=False)
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)


def _events(kind: str) -> list[dict]:
    path = data_dir() / "tow.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [row for row in rows if row.get("kind") == kind]


def test_a_new_install_gets_its_key_once_and_is_told_to_back_it_up(no_key, capsys):
    assert not key_file().exists()
    assert cli._first_run_key() == key_file()
    assert key_file().is_file()
    out = capsys.readouterr().out
    assert "master.key" in out
    assert len(_events("master_key_created")) == 1
    # Once: a key in use is never replaced and the notice is not repeated.
    before = key_file().read_bytes()
    assert cli._first_run_key() is None
    assert key_file().read_bytes() == before
    assert capsys.readouterr().out == ""
    assert len(_events("master_key_created")) == 1


def test_run_and_serve_create_the_key_before_they_start(no_key, monkeypatch):
    monkeypatch.setattr("tow.supervisor.run_supervisor", lambda: 0)
    assert cli.main(["run"]) == 0
    assert key_file().is_file()


def test_existing_secrets_without_their_key_are_never_given_a_new_one(no_key, monkeypatch, capsys):
    from tow.store import encrypted_secrets_path

    encrypted_secrets_path().parent.mkdir(parents=True, exist_ok=True)
    encrypted_secrets_path().write_text("{}", encoding="utf-8")
    assert cli._first_run_key() is None
    assert not key_file().exists()
    assert cli.main(["keys", "ensure", "--json"]) == 3
    assert "tow keys adopt --from" in json.loads(capsys.readouterr().out)["error"]


def test_a_missing_key_names_the_command_that_fixes_it(no_key):
    from tow.store import MissingMasterKeyError, SecretStoreError, _master_key

    with pytest.raises(SecretStoreError) as caught:
        _master_key()
    assert isinstance(caught.value, MissingMasterKeyError)
    assert "tow keys ensure" in str(caught.value)
    assert "tow keys adopt --from" in str(caught.value)


def test_keys_ensure_with_a_key_in_use_changes_nothing(capsys):
    assert cli.main(["keys", "ensure", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "key_file_created": False}
