"""Passkeys in magnet and udp:// announce addresses never reach the log, a Home row or a
messenger: scrub_text reduces them, and a client's own error answer is scrubbed at the source."""

from __future__ import annotations

import json

import httpx
import pytest

from tow.clients.managed import ClientError
from tow.log import _redact, scrub_text

PASSKEY = "0123456789abcdef0123456789abcdef"
HASH = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"


@pytest.mark.parametrize(
    "text",
    [
        f"magnet:?xt=urn:btih:{HASH}&dn=Show+A&tr=http%3A%2F%2Ftracker.example%2Fann%3Fpasskey%3D{PASSKEY}",
        f"magnet:?dn=Show+A&tr=udp%3A%2F%2Ftracker.example%3A6969%2F{PASSKEY}%2Fannounce&xt=urn:btih:{HASH}",
        f"magnet:?xt=urn:btih:{HASH}&x.pe=192.0.2.5:6881&tr=https://tracker.example/a?uk={PASSKEY}",
    ],
)
def test_a_magnet_link_keeps_only_its_info_hash(text):
    cleaned = scrub_text(f"add failed: {text}; try again")
    assert PASSKEY not in cleaned
    assert "tracker.example" not in cleaned
    assert "192.0.2.5" not in cleaned
    assert f"magnet:?xt=urn:btih:{HASH}" in cleaned
    assert cleaned.endswith("; try again")


@pytest.mark.parametrize(
    "text",
    [
        f"udp://tracker.example:6969/{PASSKEY}/announce",
        f"UDP://tracker.example:6969/announce?passkey={PASSKEY}",
        f"wss://tracker.example/announce?pk={PASSKEY}",
    ],
)
def test_udp_and_websocket_announce_addresses_lose_path_and_query(text):
    cleaned = scrub_text(f"tracker {text} failed")
    assert PASSKEY not in cleaned
    assert "tracker.example:6969" in cleaned or "wss://tracker.example" in cleaned


@pytest.mark.parametrize("key", ["passkey", "uk", "pk", "PassKey"])
def test_passkey_uk_and_pk_values_are_secrets(key):
    assert PASSKEY not in scrub_text(f"announce rejected ({key}={PASSKEY})")
    assert _redact({key: PASSKEY}) == {key: "***"}


def test_ordinary_words_are_kept():
    assert scrub_text("the UK pack: 3 files") == "the UK pack: 3 files"


def test_a_client_answer_quoting_a_magnet_is_scrubbed_before_it_is_stored():
    from tow.clients.deluge import DelugeClient

    magnet = f"magnet:?xt=urn:btih:{HASH}&tr=udp%3A%2F%2Ftracker.example%3A6969%2Fannounce%3Fpasskey%3D{PASSKEY}"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "auth.login":
            return httpx.Response(200, json={"result": True, "error": None, "id": body["id"]})
        if body["method"] == "web.connected":
            return httpx.Response(200, json={"result": True, "error": None, "id": body["id"]})
        error = {"message": f"invalid magnet {magnet}", "code": 5}
        return httpx.Response(200, json={"result": None, "error": error, "id": body["id"]})

    client = DelugeClient("127.0.0.1", 8112, "deluge", transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError) as caught:
        client.ping()
    assert PASSKEY not in str(caught.value)
    assert PASSKEY not in json.dumps(caught.value.params)
    assert f"magnet:?xt=urn:btih:{HASH}" in caught.value.params["answer"]
