"""Client adapters retain a server's retry hint for bounded background observation."""

from __future__ import annotations

import httpx
import pytest

from tow.clients.deluge import DelugeClient
from tow.clients.managed import ClientError
from tow.clients.transmission import TransmissionClient


@pytest.mark.parametrize("kind", ["transmission", "deluge"])
def test_typed_client_http_failure_preserves_retry_after(kind):
    transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "900"}))
    client = (
        TransmissionClient("127.0.0.1", 9091, "", "", transport=transport)
        if kind == "transmission"
        else DelugeClient("127.0.0.1", 8112, "", transport=transport)
    )
    with pytest.raises(ClientError) as error:
        client.ping()
    assert error.value.code == "client.managed.http_error"
    assert error.value.params["code"] == 429
    assert error.value.params["retry_after"] == 900
