"""TOW's torrent-client adapters against real qBittorrent, Transmission and Deluge.

Skipped unless TOW_REAL_CLIENTS=1: .github/workflows/real.yml starts each client on an Ubuntu
runner with a login of that run and sets the variables below; nothing here ever runs against the
owner's clients. Each client gets small multi-file torrents whose data already lies in a folder
the client reads. Through TOW's own adapter, built by the client factory from settings as TOW
keeps them: added STOPPED with a file selection, read back (the selection and TOW's mark: the
qBittorrent tag or the Transmission/Deluge label "tow"), started, complete after a recheck,
stopped, moved; a torrent added without the mark is adopted; then each is removed (the torrent
only, the files stay). Then the whole pipeline: a local site serves a topic and its .torrent,
`tow check --apply` hands it to the client, a new version is published and handed over too.

    TOW_REAL_QBITTORRENT, TOW_REAL_TRANSMISSION, TOW_REAL_DELUGE   host:port of each client
    TOW_REAL_CLIENT_USER, TOW_REAL_CLIENT_SECRET                   its login (Deluge: the secret)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from helpers import make_torrent, raises_code

pytestmark = [pytest.mark.real_system("TOW_REAL_CLIENTS"), pytest.mark.timeout(900)]

KINDS = ("qbittorrent", "transmission", "deluge")
PIECE = 16384
WAIT_SEC = 120  # every wait for the client is bounded; a real client on a runner may be slow
OWNER, PENDING = "tow", "tow-pending"


# --- torrents with real data ---------------------------------------------------------------------


@dataclass(frozen=True)
class Torrent:
    name: str
    files: dict[str, bytes]  # path below the torrent's folder -> its bytes, in the torrent's order
    content: bytes
    infohash: str


def _bytes(seed: str, size: int) -> bytes:
    blocks = (hashlib.sha256(f"{seed}:{index}".encode()).digest() for index in range(size // 32 + 1))
    return b"".join(blocks)[:size]


def build_torrent(name: str, sizes: dict[str, int]) -> Torrent:
    """A private v1 multi-file torrent with the real piece hashes of its synthetic files."""
    from tow.torrent import parse_torrent_metadata

    files = {path: _bytes(f"{name}/{path}", size) for path, size in sizes.items()}
    data = b"".join(files.values())
    pieces = b"".join(hashlib.sha1(data[start : start + PIECE]).digest() for start in range(0, len(data), PIECE))
    info = {
        b"name": name.encode(),
        b"piece length": PIECE,
        b"pieces": pieces,
        b"private": 1,  # no DHT or peer exchange: the client talks to nobody about it
        b"files": [
            {b"length": len(body), b"path": [part.encode() for part in path.split("/")]} for path, body in files.items()
        ],
    }
    content = make_torrent(info, announce="http://127.0.0.1:9/announce")
    return Torrent(name, files, content, parse_torrent_metadata(content).client_hash.upper())


def write_data(folder: Path, torrent: Torrent) -> None:
    for path, body in torrent.files.items():
        target = folder / torrent.name / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


# --- the client, as TOW configures it --------------------------------------------------------------


def client_settings(kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The config's client list and the secret block TOW keeps for it (Settings writes these)."""
    variable = f"TOW_REAL_{kind.upper()}"
    address = os.environ.get(variable, "")
    if ":" not in address or not os.environ.get("TOW_REAL_CLIENT_SECRET"):
        pytest.fail(f"TOW_REAL_CLIENTS=1 needs {variable} (host:port) and TOW_REAL_CLIENT_SECRET")
    host, _, port = address.rpartition(":")
    block = {
        "host": host,
        "port": int(port),
        "username": os.environ.get("TOW_REAL_CLIENT_USER", ""),
        "password": os.environ["TOW_REAL_CLIENT_SECRET"],
    }
    return {"clients": [{"id": "real", "kind": kind, "default": True}]}, {"clients": {"real": block}}


def adapter_for(kind: str) -> Any:
    from tow.clients.factory import from_secrets

    cfg, secrets = client_settings(kind)
    return from_secrets(cfg, secrets, "real")


class Raw:
    """What TOW never does itself, done with the client's own API over the adapter's connection:
    add a torrent without TOW's mark, recheck its data, remove it (never its files)."""

    def __init__(self, kind: str, adapter: Any) -> None:
        self.kind = kind
        self.adapter = adapter

    def add_unmarked(self, content: bytes, folder: Path) -> None:
        encoded = base64.b64encode(content).decode("ascii")
        if self.kind == "qbittorrent":
            self.adapter._c.torrents_add(
                torrent_files=content,
                save_path=str(folder),
                is_stopped=True,
                use_auto_torrent_management=False,
                content_layout="Original",
            )
        elif self.kind == "transmission":
            self.adapter._rpc("torrent-add", metainfo=encoded, paused=True, **{"download-dir": str(folder)})
        else:
            options = {"add_paused": True, "download_location": str(folder)}
            self.adapter._call("core.add_torrent_file", "unmarked.torrent", encoded, options)

    def recheck(self, infohash: str) -> None:
        if self.kind == "qbittorrent":
            self.adapter._c.torrents_recheck(torrent_hashes=infohash.lower())
        elif self.kind == "transmission":
            self.adapter._rpc("torrent-verify", ids=[infohash.lower()])
        else:
            self.adapter._call("core.force_recheck", [infohash.lower()])

    def listing(self) -> list[Any]:
        """Every torrent in the client (hash, name, state, mark), for a failure message."""
        try:
            if self.kind == "qbittorrent":
                return [(t.hash, t.name, t.state, t.tags) for t in self.adapter._c.torrents_info()]
            if self.kind == "transmission":
                fields = ["hashString", "name", "status", "percentDone", "labels"]
                return list(self.adapter._rpc("torrent-get", fields=fields).get("torrents") or [])
            keys = ["hash", "name", "state", "progress", "label"]
            return list((self.adapter._call("core.get_torrents_status", {}, keys) or {}).values())
        except Exception as exc:  # noqa: BLE001 - only a failure message: the client's own error is shown
            return [f"the client's torrent list is unreadable: {type(exc).__name__}: {exc}"]

    def remove(self, infohash: str) -> None:
        if self.kind == "qbittorrent":
            self.adapter._c.torrents_delete(delete_files=False, torrent_hashes=infohash.lower())
        elif self.kind == "transmission":
            self.adapter._rpc("torrent-remove", ids=[infohash.lower()], **{"delete-local-data": False})
        else:
            self.adapter._call("core.remove_torrent", infohash.lower(), False)


def wait_until(what: str, probe: Callable[[], Any], show: Callable[[], Any], seconds: float = WAIT_SEC) -> Any:
    """``probe()`` until it is truthy; else fail with what the client shows (``show()``)."""
    deadline = time.monotonic() + seconds
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not within {seconds} s; the client shows {show()!r}")
        time.sleep(0.5)


def tags(info: dict[str, Any] | None) -> set[str]:
    return {str(tag).strip().casefold() for tag in (info or {}).get("tags") or []}


def wanted(info: dict[str, Any] | None) -> dict[str, bool]:
    """Each file below the torrent's folder: does the client download it?"""
    rows = (info or {}).get("files") or []
    return {str(row["name"]).replace("\\", "/").split("/", 1)[-1]: int(row.get("priority") or 0) > 0 for row in rows}


def stopped(info: dict[str, Any] | None) -> bool:
    return str((info or {}).get("state") or "").casefold().startswith(("stopped", "paused"))


def complete(info: dict[str, Any] | None) -> bool:
    state = str((info or {}).get("state") or "").casefold()
    return info is not None and float(info.get("progress") or 0) >= 1.0 and not state.startswith(("checking", "moving"))


def summary(info: dict[str, Any] | None) -> Any:
    keys = ("state", "progress", "save_path", "tags")
    return None if info is None else {**{key: info.get(key) for key in keys}, "wanted": wanted(info)}


# --- the adapter, step by step -----------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_the_adapter_adds_selects_starts_moves_adopts_in_the_real_client(kind, tmp_path):
    from tow.folders import paths_equal

    adapter = adapter_for(kind)
    raw = Raw(kind, adapter)
    assert adapter.ping()
    data, moved = tmp_path / "data", tmp_path / "moved"
    moved.mkdir()
    torrent = build_torrent(f"tow-real-{kind}", {"a/e01.mkv": 40_000, "e02.mkv": 30_000, "notes.txt": 5_000})
    unmarked = build_torrent(f"tow-real-{kind}-own", {"x.mkv": 20_000, "y.txt": 1_000})
    for one in (torrent, unmarked):
        write_data(data, one)
    h = torrent.infohash

    def show() -> Any:
        return summary(adapter.inspect_torrent(h))

    added: list[str] = []
    try:
        # 1. Added STOPPED with two of its three files, confirmed by read-back (TOW's add).
        adapter.add_torrent_selected(torrent.content, str(data), h, [0, 2], start=False)
        added.append(h)
        info = adapter.inspect_torrent(h)
        assert stopped(info), show()
        assert wanted(info) == {"a/e01.mkv": True, "e02.mkv": False, "notes.txt": True}, show()
        assert OWNER in tags(info), show()
        assert PENDING not in tags(info), show()
        assert paths_equal(str(info["save_path"]), str(data)), show()

        # 2. Started by TOW; its data is there, so after a recheck it is complete.
        adapter.start_owned_torrent(h)  # confirmed running (or complete) by read-back
        raw.recheck(h)
        wait_until("complete after a recheck", lambda: complete(adapter.inspect_torrent(h)), show)
        assert wanted(adapter.inspect_torrent(h))["e02.mkv"] is False, show()

        # 3. Stopped by TOW (read back).
        assert stopped(adapter.stop_owned_torrent(h)), show()

        # 4. Moved to another folder; the client moves the files.
        assert adapter.set_location(h, str(moved)) == "ok"
        wait_until(
            f"moved to {moved}",
            lambda: (
                (info := adapter.inspect_torrent(h)) is not None
                and paths_equal(str(info["save_path"]), str(moved))
                and "moving" not in str(info.get("state")).casefold()
            ),
            show,
        )
        wait_until("the files in the new folder", lambda: (moved / torrent.name / "a" / "e01.mkv").is_file(), show)
        assert OWNER in tags(adapter.inspect_torrent(h)), show()

        # 5. A torrent the owner added without TOW's mark: TOW does not touch it until adopted.
        raw.add_unmarked(unmarked.content, data)
        added.append(unmarked.infohash)
        own = wait_until(
            "the owner's torrent listed",
            lambda: adapter.inspect_torrent(unmarked.infohash),
            lambda: summary(adapter.inspect_torrent(unmarked.infohash)),
        )
        assert OWNER not in tags(own)
        with raises_code("client.managed.not_owned_stop"):
            adapter.stop_owned_torrent(unmarked.infohash)
        assert OWNER in {tag.casefold() for tag in adapter.adopt_torrent(unmarked.infohash)}
        adopted = adapter.inspect_torrent(unmarked.infohash)
        assert OWNER in tags(adopted), summary(adopted)
        assert wanted(adopted) == {"x.mkv": True, "y.txt": True}, summary(adopted)  # nothing else changed

        # 6. Removed (the torrent only): gone from the client, the files stay.
        for infohash in list(added):
            raw.remove(infohash)
            wait_until(f"{infohash} removed", lambda infohash=infohash: adapter.inspect_torrent(infohash) is None, show)
            added.remove(infohash)
        assert (moved / torrent.name / "a" / "e01.mkv").read_bytes() == torrent.files["a/e01.mkv"]
        assert (data / unmarked.name / "x.mkv").is_file()
    finally:
        for infohash in added:
            with suppress(Exception):
                raw.remove(infohash)


# --- the whole pipeline: a site, `tow check --apply`, the client ------------------------------------


class Site:
    """A forum with one topic: its page, and /download/1 with the topic's current .torrent."""

    def __init__(self) -> None:
        self.torrent = b""
        self.asked: list[str] = []
        self.url = ""

    def answer(self, path: str) -> tuple[int, bytes, str]:
        self.asked.append(path)
        if path == "/topic/1":
            page = "<html><head><title>Show S01 :: Real test site</title></head><body>"
            page += '<h1>Show S01</h1><a href="/download/1">Download .torrent</a></body></html>'
            return 200, page.encode(), "text/html; charset=utf-8"
        if path == "/download/1" and self.torrent:
            return 200, self.torrent, "application/x-bittorrent"
        return 404, b"Not Found", "text/plain"


@contextmanager
def local_site() -> Iterator[Site]:
    site = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, body, kind = site.answer(self.path)
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    site.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def configure_install(kind: str, site: Site, downloads: Path) -> None:
    """The test's own TOW_ROOT (tests/conftest.py): the site, the client, one topic."""
    from tow.config import load_config, save_config
    from tow.store import save_secrets, save_state

    clients, secrets = client_settings(kind)
    port = site.url.rsplit(":", 1)[1]
    cfg = load_config()
    cfg.update(clients)
    cfg["allow_private_tracker_hosts"] = True  # the site is on this computer
    cfg["trackers"] = {
        "realsite": {
            "title": "Real test site",
            "url_regex": rf"^http://127\.0\.0\.1:{port}/topic/(\d+)$",
            "fetch_hosts": [site.url],
            "download_path": "/download/{id}",
        }
    }
    save_config(cfg)
    save_secrets(secrets)
    topic = {
        "id": "real1",
        "title": "Show S01",
        "url": f"{site.url}/topic/1",
        "save_path": str(downloads),
        "hash": None,
        "client_id": "real",
        "selection": {"mode": "files", "value": "*.mkv"},
        "tracking_mode": "watch",
    }
    save_state({"topics": [topic]})


def check_apply(capsys, raw: Raw, *, ok: bool = True) -> dict[str, Any]:
    """`tow check --apply` as the owner runs it (in this process: the guard is off here). The
    whole report and the client's torrent list are in the message when it is not as expected."""
    from tow import cli

    capsys.readouterr()
    code = cli.main(["check", "--apply", "--manual", "--json"])
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{") :])
    rows = report.get("results") or []
    expected = (0, [True]) if ok else (2, [False])
    assert (code, [bool(row.get("ok")) for row in rows]) == expected, f"{out}; the client holds: {raw.listing()!r}"
    return rows[0]


@pytest.mark.parametrize("kind", KINDS)
def test_tow_check_apply_hands_each_new_version_to_the_real_client(kind, tmp_path, capsys):
    """A new version whose files the previous one still seeds is refused (G1) until the owner
    stops the previous one with TOW's own action, "Stop the previous one and add"
    (POST /topics/<id>/replace-revision): it stops it (kept in the client) and adds the new one."""
    from fastapi.testclient import TestClient
    from helpers import shown

    from tow.check import PREVIOUS_REVISION_ACTIVE
    from tow.store import load_state
    from tow.web import app

    adapter = adapter_for(kind)
    raw = Raw(kind, adapter)
    downloads = tmp_path / "downloads"
    sizes = {"Show.S01E01.mkv": 30_000, "Show.S01E02.mkv": 30_000, "notes.txt": 2_000}
    first = build_torrent("Show S01", sizes)
    second = build_torrent("Show S01", {**sizes, "Show.S01E03.mkv": 30_000})
    for version in (first, second):
        write_data(downloads, version)
    assert first.infohash != second.infohash

    def held(version: Torrent) -> dict[str, Any]:
        info = adapter.inspect_torrent(version.infohash)
        listing = f"the client holds: {raw.listing()!r}"
        assert info is not None, f"the client does not hold {version.infohash}; {listing}"
        assert OWNER in tags(info), f"{summary(info)}; {listing}"
        assert PENDING not in tags(info), f"{summary(info)}; {listing}"
        return info

    def topic() -> dict[str, Any]:
        return load_state()["topics"][0]

    with local_site() as site:
        configure_install(kind, site, downloads)
        try:
            site.torrent = first.content
            row = check_apply(capsys, raw)
            assert str(row.get("hash")).upper() == first.infohash, row
            info = held(first)
            assert wanted(info) == {"Show.S01E01.mkv": True, "Show.S01E02.mkv": True, "notes.txt": False}
            assert not stopped(info), summary(info)  # started: it seeds its files
            assert str(topic()["hash"]).upper() == first.infohash

            # The site replaces the topic's torrent with a new version. The first one still seeds
            # the same files: the check refuses to add beside it and says why.
            site.torrent = second.content
            check_apply(capsys, raw, ok=False)
            assert topic().get("last_error_code") == PREVIOUS_REVISION_ACTIVE, topic()
            assert adapter.inspect_torrent(second.infohash) is None, raw.listing()
            assert str(topic()["hash"]).upper() == first.infohash

            # The owner's way forward: "Stop the previous one and add" - TOW stops the first
            # version (it stays in the client) and checks the topic again, which adds the new one.
            browser = TestClient(app, headers={"Origin": "http://127.0.0.1"})
            response = browser.post(f"/topics/{topic()['id']}/replace-revision", follow_redirects=False)
            said = shown(response.headers.get("location", ""))
            assert response.status_code == 303, said
            info = held(second)
            assert wanted(info) == {
                "Show.S01E01.mkv": True,
                "Show.S01E02.mkv": True,
                "Show.S01E03.mkv": True,
                "notes.txt": False,
            }, f"{summary(info)}; {said}"
            previous = adapter.inspect_torrent(first.infohash)
            assert previous is not None, f"the previous version was removed, not stopped; {raw.listing()!r}"
            assert stopped(previous), summary(previous)
            assert str(topic()["hash"]).upper() == second.infohash, (topic(), said)
            assert not topic().get("last_error_code"), (topic(), said)

            # And a check by hand afterwards finds everything as it should be.
            row = check_apply(capsys, raw)
            assert str(row.get("hash")).upper() == second.infohash, row
            held(second)
            assert stopped(adapter.inspect_torrent(first.infohash)), raw.listing()
            assert "/download/1" in site.asked
        finally:
            for version in (first, second):
                with suppress(Exception):
                    raw.remove(version.infohash)
