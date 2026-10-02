"""Shared test helpers (import as `from helpers import ...`)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def bencode(value: Any) -> bytes:
    """Canonical bencode (sorted dict keys); str is encoded as UTF-8."""
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"
    raise TypeError(value)


def make_torrent(info: dict[bytes, Any], **root: Any) -> bytes:
    """A .torrent with this info dictionary; keyword arguments become root keys (announce=..., comment=...)."""
    return bencode({**{key.encode(): value for key, value in root.items()}, b"info": info})


def multi_file_torrent(
    name: bytes, files: list[dict[bytes, Any]], *, piece_length: int = 16384, pieces: bytes = b"x" * 20
) -> bytes:
    """A v1 multi-file torrent; the default single fake piece hash suits content up to one piece."""
    return make_torrent({b"files": files, b"name": name, b"piece length": piece_length, b"pieces": pieces})


def flash_record(location: str) -> dict[str, str] | None:
    """The message (``{"text", "kind"}``) a redirect to ``location`` shows: the server keeps it,
    the address carries ``flash=<token>``."""
    from urllib.parse import parse_qs, urlparse

    from tow.web.views import _FLASHES

    token = (parse_qs(urlparse(location).query).get("flash") or [""])[0]
    return _FLASHES.get(token) if token else None


def flash_of(location: str) -> str:
    """The text of the message a redirect to ``location`` shows ("" for none)."""
    record = flash_record(location)
    return record["text"] if record else ""


def shown(location: str) -> str:
    """A redirect as the owner meets it: the decoded address and the message it shows."""
    from urllib.parse import unquote_plus

    return f"{unquote_plus(location)} {flash_of(location)}"


def flash_kind(location: str) -> str:
    record = flash_record(location)
    return record["kind"] if record else ""


def open_network(monkeypatch: Any = None, *, token: str = "") -> None:
    """Network access on in config.yaml, as Settings turns it on; ``token`` also sets up the
    external access key (TOW_LAN_AUTH_TOKEN), which counts only while no password is set."""
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    if token:
        monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", token)


@contextmanager
def raises_code(code: str, kind: type[BaseException] = Exception) -> Iterator[Any]:
    """Like ``pytest.raises(kind)``, and the error is the typed TOW error ``code`` (its key in the
    language files): tests check what failed, not the wording."""
    import pytest

    with pytest.raises(kind) as caught:
        yield caught
    assert getattr(caught.value, "code", None) == code, (code, caught.value)


@contextmanager
def reaped(*processes: Any, timeout: float = 10) -> Iterator[None]:
    """Child processes never outlive the test, even when an assertion fails mid-way.

    On exit every started process is joined with a timeout and killed if still alive,
    so a failed test cannot leave a child blocked on a lock (pytest would then hang at
    exit waiting for it).
    """
    try:
        yield
    finally:
        for process in processes:
            if process.pid is None:  # never started
                continue
            process.join(timeout)
            if process.is_alive():
                process.kill()
                process.join(5)


def add_unready_client(monkeypatch: Any, kind: str = "draft") -> None:
    """A client module that exists but is not ready (READY = False): listed nowhere, refused everywhere."""
    from tow.clients import spec

    found = spec._discover()

    def load(secrets: dict[str, Any]) -> Any:
        raise AssertionError("an unready client is never loaded")

    draft = spec.ClientSpec(kind=kind, title=kind.title(), secrets_key=kind, ready=False, default_port=8080, load=load)
    monkeypatch.setattr(spec, "_discover", lambda: (*found, (kind, draft)))


def wait_for_check_job(client: Any, job: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Poll /check/status until the background check ``job`` ends; fail the test if it never does."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        body: dict[str, Any] = client.get(f"/check/status?job={job}").json()
        if body["status"] != "running":
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"background check {job} still running after {timeout} s")
        time.sleep(0.02)
