"""Sessions fail closed when ``data/sessions.json`` cannot be trusted.

The cookies here are written out in full (their signatures computed once, outside the code
under test), so a change of the signing scheme, of the epoch a damaged or unreadable file falls
back to, or of the expiry window shows up as a refused or an accepted cookie. An epoch-0 cookie
is what every device had before the first "Sign out everywhere" - one that must stop working
after it, even when the file that says so is damaged or held by a writer.
"""

from __future__ import annotations

import json

import pytest

from tow.auth import (
    SESSION_TTL_SEC,
    AuthConfigurationError,
    clear_sessions,
    issue_session,
    revoke_session,
    session_is_valid,
    sign_out_everywhere,
)
from tow.paths import data_dir

TOKEN = "t" * 32
NOW = 1_000_000_000.0
PAYLOAD = "Session-made-for-this-test-0123456789.1000086400"  # expires a day after NOW
EPOCH_0 = f"{PAYLOAD}.K8-O5EEuwyAW5uoO75koqA4qrMhxiQrx2TdR3npSawk"
EPOCH_1 = f"{PAYLOAD}.4dohzadm-L3c7uC8kH5GZCMfsw22tkIo2VjLrY8E7Pw"
EPOCH_2 = f"{PAYLOAD}.SQwH5RnbIBWSod_xO2wy3kd7usQyRQxYkUyJf6DDPes"


def _sessions_file():
    return data_dir() / "sessions.json"


def test_each_epoch_signs_with_its_own_key():
    assert session_is_valid(EPOCH_0, TOKEN, now=NOW)  # no file: epoch 0, the signature of every version
    assert not session_is_valid(EPOCH_1, TOKEN, now=NOW)
    assert sign_out_everywhere() == 1
    assert not session_is_valid(EPOCH_0, TOKEN, now=NOW)
    assert session_is_valid(EPOCH_1, TOKEN, now=NOW)
    assert sign_out_everywhere() == 2
    assert [session_is_valid(c, TOKEN, now=NOW) for c in (EPOCH_0, EPOCH_1, EPOCH_2)] == [False, False, True]
    assert not session_is_valid(EPOCH_2, "u" * 32, now=NOW)


@pytest.mark.parametrize("content", [b"{broken", b"\xff\xfe", b'{"epoch": "one"}', b"[1, 2]", b'{"revoked": [1]}'])
def test_a_damaged_sessions_file_refuses_even_the_oldest_cookies(content):
    sign_out_everywhere()
    _sessions_file().write_bytes(content)
    clear_sessions()

    assert not session_is_valid(EPOCH_0, TOKEN, now=NOW)
    assert not session_is_valid(EPOCH_1, TOKEN, now=NOW)


def test_a_damaged_sessions_file_without_any_sign_out_still_refuses_epoch_0():
    data_dir().mkdir(parents=True, exist_ok=True)
    _sessions_file().write_text("{broken", encoding="utf-8")
    clear_sessions()

    assert not session_is_valid(EPOCH_0, TOKEN, now=NOW)


def test_a_sessions_file_that_cannot_be_read_and_was_never_read_refuses_every_cookie():
    sign_out_everywhere()
    _sessions_file().unlink()
    _sessions_file().mkdir()  # it exists, but reading it fails (as a file a writer holds does)
    clear_sessions()  # nothing known about it in this process

    assert not session_is_valid(EPOCH_0, TOKEN, now=NOW)
    assert not session_is_valid(EPOCH_1, TOKEN, now=NOW)


def test_a_cookie_is_valid_until_it_expires_and_never_from_too_far_ahead():
    expires = int(PAYLOAD.rsplit(".", 1)[1])
    assert session_is_valid(EPOCH_0, TOKEN, now=expires - 1)
    assert not session_is_valid(EPOCH_0, TOKEN, now=expires)
    assert session_is_valid(EPOCH_0, TOKEN, now=expires - SESSION_TTL_SEC - 60)  # a clock a minute behind
    assert not session_is_valid(EPOCH_0, TOKEN, now=expires - SESSION_TTL_SEC - 61)


@pytest.mark.parametrize(
    "cookie",
    [
        None,
        "",
        PAYLOAD,
        f"{EPOCH_0}.x",
        EPOCH_0.replace("Session-made", "Session made"),
        EPOCH_0.replace(".1000086400.", ".-1000086400."),
        f"short.1000086400.{EPOCH_0.rsplit('.', 1)[1]}",
        f"{PAYLOAD}.short",
        f"{PAYLOAD}.{'A' * 129}",
        "S" * 200 + EPOCH_0,
    ],
)
def test_a_malformed_cookie_is_refused(cookie):
    assert not session_is_valid(cookie, TOKEN, now=NOW)


def test_a_session_is_never_issued_with_a_short_key():
    with pytest.raises(AuthConfigurationError):
        issue_session("t" * 31)


def test_a_signed_out_session_is_remembered_until_its_own_expiry():
    revoke_session(EPOCH_0, TOKEN, now=NOW)

    stored = json.loads(_sessions_file().read_text(encoding="utf-8"))
    assert stored["epoch"] == 0
    assert list(stored["revoked"].values()) == [int(PAYLOAD.rsplit(".", 1)[1])]
    assert not session_is_valid(EPOCH_0, TOKEN, now=NOW)
    clear_sessions()
    assert not session_is_valid(EPOCH_0, TOKEN, now=NOW)
