"""No device - this computer included - is shown the credentials in a link of an error (1.33.1
QA-16): a link's sign-in (user:password@), its secret query values (uk, pk, passkey, token...),
a tracker announce address (its passkey) and a header's or a name=value secret go everywhere an
error or a diagnostic is shown; a network device also loses folders and keeps only a link's host.
New errors are stored without them too; stored ones from before are cleaned when shown."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from helpers import open_network

from tow import errors, log
from tow.check import rows as check_rows
from tow.config import load_config, save_config
from tow.errors import TowError
from tow.scrub import credentials_out, scrub_credentials, scrub_display
from tow.snapshots.record import record_status
from tow.store import save_state
from tow.web import app

SECRETS = ("SECRETUK", "SECRETPK", "hunter2", "PASSKEY0123456789abcdef", "tok-abc-123")
# 1.33.1 review: a query name written with %-escapes, a diagnostic echoing JSON or a Python
# mapping (quoted names), one cut off inside its value, and a secret by its key among the values.
SECRETS += ("ENCODEDPK42", "JSONTOKEN77", "PYPASSWORD88", "CUTAPIKEY99", "MAPTOKEN55")
LINK = "https://owner:hunter2@tracker.example/dl.php?t=5&uk=SECRETUK&pk=SECRETPK"
ANNOUNCE = "http://bt.example/PASSKEY0123456789abcdef/announce"
ENCODED = "https://tracker.example/dl.php?t=6&%70k=ENCODEDPK42"
ECHOED = '{"token":"JSONTOKEN77","ok":false} {\'password\': \'PYPASSWORD88\'} {"api_key":"CUTAPIKEY99'
PLAIN = f"HTTP 403 from {LINK} (announce {ANNOUNCE}, token=tok-abc-123) via {ENCODED} said {ECHOED}"


def _leaks(text: str) -> list[str]:
    return [secret for secret in SECRETS if secret in text]


def test_the_credentials_scrub_keeps_the_rest_of_the_text():
    shown = scrub_credentials(PLAIN)
    assert _leaks(shown) == []
    assert "https://tracker.example/dl.php?t=5&uk=***&pk=***" in shown  # the link stays useful
    assert "http://bt.example" in shown
    assert "token=***" in shown
    assert scrub_credentials(r"D:\TV\Show A\e01.mkv was not found") == r"D:\TV\Show A\e01.mkv was not found"
    assert scrub_credentials("magnet:?xt=urn:btih:abc&tr=http://t.example/PASSKEY0123456789abcdef/announce") == (
        "magnet:?xt=urn:btih:abc"
    )
    assert scrub_credentials("Authorization: Bearer xyz") == "Authorization: ***"
    assert "dl.php?t=6&%70k=***" in shown  # the name as written, its value gone
    assert '{"token":"***","ok":false}' in shown  # the echo stays readable
    assert "{'password': '***'}" in shown
    assert shown.endswith('{"api_key":"***"')
    assert scrub_credentials("the token is fine, a key=value stays") == "the token is fine, a key=value stays"
    network = scrub_display(PLAIN + r" in D:\TV\Show A")
    assert _leaks(network) == []
    assert r"D:\TV" not in network


def test_message_values_lose_their_credentials_in_messages_inside_messages():
    params = {"tracker": "rutor", "error": {"$msg": {"code": "mirrors.local", "params": {"error": PLAIN}}}}
    assert _leaks(str(credentials_out(params))) == []
    assert credentials_out(params)["tracker"] == "rutor"


def test_a_value_named_as_a_secret_is_dropped_whatever_it_holds():
    params = {
        "tracker": "rutor",
        "token": "MAPTOKEN55",
        "Bot-Token": "MAPTOKEN55",
        "credentials": {"password": 123, "user": "owner"},
        "cookies": ["MAPTOKEN55"],
        "error": {"$msg": {"code": "mirrors.local", "params": {"api_key": "MAPTOKEN55", "error": "x"}}},
        "key": "settings.title",  # a catalog key, no secret: "key" alone says too little
        "pass": None,
    }
    out = credentials_out(params)
    assert _leaks(str(out)) == []
    assert out["token"] == out["Bot-Token"] == out["cookies"] == "***"
    assert out["credentials"] == {"password": "***", "user": "owner"}
    assert out["error"]["$msg"]["params"] == {"api_key": "***", "error": "x"}  # a message stays one
    assert (out["tracker"], out["key"], out["pass"]) == ("rutor", "settings.title", None)
    assert params["token"] == "MAPTOKEN55"  # the caller's values are never changed


def test_a_checks_error_is_stored_without_credentials():
    row: dict = {}
    check_rows.set_error(row, RuntimeError(PLAIN))
    assert _leaks(str(row)) == []
    typed: dict = {}
    check_rows.set_error(typed, TowError("mirrors.local", tracker="rutor", error=PLAIN, token="MAPTOKEN55"))
    assert _leaks(str(typed)) == []
    assert typed["error_record"]["code"] == "mirrors.local"


def test_rendered_errors_never_carry_credentials():
    record = {"code": "mirrors.local", "params": {"tracker": "rutor", "error": PLAIN}}
    assert _leaks(errors.render(record, "en")) == []
    assert _leaks(errors.render_stored(None, None, PLAIN, "en")) == []
    assert _leaks(errors.text_of(PLAIN)) == []


@pytest.fixture
def planted(monkeypatch):
    """Errors saved by an older TOW (with the credentials in them) on every kind of record."""
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)
    cfg = load_config()
    cfg["trackers"] = {"club": {"title": "Club", "url_regex": r"https://club\.example/t(\d+)"}}
    save_config(cfg)
    save_state(
        {
            "topics": [
                {
                    "id": "t1",
                    "title": "Show A",
                    "url": "https://club.example/t1",
                    "last_error": PLAIN,
                    "last_error_class": "unknown",
                    "last_check": "2026-10-17T10:00:00+00:00",
                },
                {
                    "id": "t2",
                    "title": "Show B",
                    "url": "https://club.example/t2",
                    "last_error": "stored text",
                    "last_error_code": "mirrors.local",
                    "last_error_params": {"tracker": "club", "error": PLAIN},
                    "last_error_class": "tracker",
                    "last_check": "2026-10-17T10:00:00+00:00",
                },
            ],
            "mirrors": {},
        }
    )
    moment = datetime.now(UTC).isoformat()
    with log.log_path().open("a", encoding="utf-8") as handle:  # as an older TOW wrote them
        for kind, fields in (
            ("check_fail", {"topic": "t1", "tracker": "club", "error": PLAIN, "cls": "unknown", "how": "auto"}),
            (
                "check_fail",
                {
                    "topic": "t2",
                    "tracker": "club",
                    "error": "x",
                    "error_code": "mirrors.local",
                    "error_params": {"tracker": "club", "error": PLAIN},
                    "cls": "tracker",
                },
            ),
            ("backup_failed", {"error": PLAIN, "how": "auto"}),
        ):
            record = {"ts": moment, "created_at": moment, "event_id": uuid.uuid4().hex, "kind": kind, **fields}
            handle.write(json.dumps(record) + "\n")
    record_status(last_error=PLAIN, last_error_at=1.0)


SURFACES = (
    "/",
    "/topics/t1/edit-panel",
    "/topics/t2/edit-panel",
    "/topics/t1/edit",
    "/topics/t2/edit",
    "/history",
    "/log.json",
    "/messages.json",
    "/settings",
    "/sites",
    "/sync.json",
    "/sync.json?page=%2Fhistory",
    "/sync.json?page=%2Fsettings",
)


def test_this_computer_is_never_shown_the_credentials(planted):
    local = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    for path in SURFACES:
        page = local.get(path).text
        assert _leaks(page) == [], path
    assert "tracker.example/dl.php" in local.get("/topics/t1/edit-panel").text  # the rest stays


def test_a_network_device_is_never_shown_the_credentials(planted, monkeypatch):
    open_network(monkeypatch, token="t" * 32)
    lan = TestClient(app, client=("192.168.1.7", 50000), base_url="http://192.168.1.2:8787")
    signed = lan.post("/login", data={"password": "t" * 32}, headers={"Origin": "http://192.168.1.2:8787"})
    assert signed.status_code in (200, 303)
    for path in SURFACES:
        page = lan.get(path).text
        assert _leaks(page) == [], path
        assert "tracker.example/dl.php" not in page, path  # a network device keeps only the host
