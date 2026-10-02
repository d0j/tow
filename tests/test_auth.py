from __future__ import annotations

from pathlib import Path

import pytest

from tow.auth import (
    AuthConfigurationError,
    clear_sessions,
    hash_lan_password,
    issue_session,
    lan_password_matches,
    load_lan_auth_token,
    revoke_session,
    session_is_valid,
)

TOKEN = "t" * 32


def test_lan_auth_token_requires_external_provisioning(monkeypatch, tmp_path):
    monkeypatch.delenv("TOW_LAN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN_FILE", str(tmp_path / "missing.token"))

    with pytest.raises(AuthConfigurationError):
        load_lan_auth_token()


def test_optional_lan_password_is_hashed_and_verifiable():
    record = hash_lan_password("correct horse battery staple")

    assert record["scheme"] == "pbkdf2-sha256"
    assert record["digest"] != "correct horse battery staple"
    assert lan_password_matches("correct horse battery staple", record)
    assert not lan_password_matches("wrong password", record)


def test_lan_auth_token_reads_file_without_logging_or_persisting_value(monkeypatch, tmp_path):
    token_file = tmp_path / "lan.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.delenv("TOW_LAN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN_FILE", str(token_file))

    assert load_lan_auth_token() == TOKEN


def test_session_is_bound_to_token_expires_and_can_be_revoked():
    clear_sessions()
    cookie = issue_session(TOKEN, now=100.0)

    assert session_is_valid(cookie, TOKEN, now=100.0)
    assert not session_is_valid(cookie, "wrong-token", now=100.0)
    assert session_is_valid(cookie, TOKEN, now=100.0 + 60.0)
    assert session_is_valid(cookie, TOKEN, now=100.0 + 89 * 24 * 3600)  # a device stays signed in for 90 days
    assert not session_is_valid(cookie, TOKEN, now=100.0 + 90 * 24 * 3600 + 1)

    fresh_cookie = issue_session(TOKEN, now=100.0)
    revoke_session(fresh_cookie)
    assert not session_is_valid(fresh_cookie, TOKEN, now=100.0)
    clear_sessions()


def test_session_survives_a_restart_and_resists_tampering():
    from tow import auth

    cookie = issue_session(TOKEN, now=100.0)
    auth.clear_sessions()  # nothing is kept in memory: a restarted TOW still accepts the device
    assert session_is_valid(cookie, TOKEN, now=200.0)
    session_id, expires, signature = cookie.split(".")
    longer = f"{session_id}.{int(expires) + 10**6}.{signature}"
    assert not session_is_valid(longer, TOKEN, now=200.0)  # the expiry is signed
    assert not session_is_valid(cookie + "x", TOKEN, now=200.0)
    assert not session_is_valid("a.b", TOKEN, now=200.0)
    assert not session_is_valid(None, TOKEN, now=200.0)


def test_windows_launcher_uses_project_relative_runtime_root():
    launcher = Path(__file__).parents[1] / "scripts" / "tow-env.cmd"
    text = launcher.read_text(encoding="utf-8")

    assert "%LOCALAPPDATA%" not in text
    assert "%APPDATA%" not in text
    assert 'set "TOW_HOME=%TOW_ROOT%\\data"' in text


def test_the_access_key_file_in_the_data_folder_is_found_without_a_launcher(monkeypatch):
    """data/lan-auth.token counts on every system and every start (before 1.21: tow-env.cmd only);
    the variables still win over it."""
    from tow.paths import data_dir

    monkeypatch.delenv("TOW_LAN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TOW_LAN_AUTH_TOKEN_FILE", raising=False)
    with pytest.raises(AuthConfigurationError) as missing:
        load_lan_auth_token()
    assert missing.value.code == "auth.token_missing"

    (data_dir() / "lan-auth.token").write_text(TOKEN + "\n", encoding="utf-8")
    assert load_lan_auth_token() == TOKEN
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "e" * 32)
    assert load_lan_auth_token() == "e" * 32
    monkeypatch.delenv("TOW_LAN_AUTH_TOKEN")
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN_FILE", str(data_dir() / "elsewhere.token"))
    with pytest.raises(AuthConfigurationError):  # an explicit file that is missing is not replaced
        load_lan_auth_token()


def test_malformed_non_ascii_session_cookie_fails_closed():
    clear_sessions()
    assert not session_is_valid("é.invalid", TOKEN)


def test_paths_fail_closed_without_explicit_or_project_runtime(monkeypatch, tmp_path):
    from tow import paths

    monkeypatch.delenv("TOW_HOME", raising=False)
    monkeypatch.delenv("TOPIC_WATCH_HOME", raising=False)
    monkeypatch.delenv("TOW_CONFIG", raising=False)
    monkeypatch.delenv("TOW_ROOT", raising=False)
    root = tmp_path / "unknown-root"  # neither a checkout nor an app/ folder of an install
    monkeypatch.setattr(paths, "repo_root", lambda: root)

    with pytest.raises(RuntimeError, match="TOW_ROOT"):
        paths.data_dir()
    with pytest.raises(RuntimeError, match="TOW_ROOT"):
        paths.config_path()
    assert not root.exists()
