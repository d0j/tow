"""How real clients spell the file names Windows reserves (B36), recorded; then TOW's keys checked.

``test_record_the_spellings_of_reserved_names`` (TOW_REAL_CLIENTS=1, and TOW_REAL_CLIENT_NAMES_OUT
naming a folder) gives each case of ``client_names.CASES`` to every client in the environment
(TOW_REAL_QBITTORRENT, TOW_REAL_TRANSMISSION) as its own private torrent with synthetic bytes and
an unreachable announce address - never a tracker, never a peer - and records per case:

1. ``listed``: the name the client lists for the file (TOW's own adapter reads it), the torrent
   added STOPPED into an empty folder;
2. ``created``: the files the client creates in that folder once started (qBittorrent with
   "pre-allocate all files" on, restored afterwards; Transmission creates none without data);
3. ``reads_torrent_spelling``: with the bytes already written under the torrent's own spelling
   (on Windows through ``\\\\?\\``, which keeps CON or a trailing dot), the client's check of
   them completes the file; if not, ``reads_listed_spelling`` with the bytes under the listed one.

Each client's record is <client>-<os>.json in that folder (an artifact of real.yml). Every torrent
is removed again; its files stay in the test's temporary folder.

``test_tow_keys_agree_with_the_recorded_spellings`` (TOW_REAL_CLIENT_NAMES=1, the records in
TOW_REAL_CLIENT_NAMES_DIR) is TOW's matching against those observations
(``client_names.mismatches``). Without records the conclusion stays the narrowed one that
tests/test_torrent.py pins: no client spelling at hand extends the device set.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from client_names import CASES, folder_of, mismatches, on_disk, torrent_of, write_spelling

pytestmark = [pytest.mark.real_system("TOW_REAL_CLIENTS"), pytest.mark.timeout(900)]

WAIT_SEC = 90


def _os() -> str:
    return "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"


class Client:
    """The client's own calls TOW never makes itself, over the adapter's connection."""

    def __init__(self, kind: str) -> None:
        from tow.clients.factory import from_secrets

        address = os.environ[f"TOW_REAL_{kind.upper()}"]
        host, _, port = address.rpartition(":")
        block = {
            "host": host,
            "port": int(port),
            "username": os.environ.get("TOW_REAL_CLIENT_USER", ""),
            "password": os.environ["TOW_REAL_CLIENT_SECRET"],
        }
        cfg = {"clients": [{"id": "real", "kind": kind, "default": True}]}
        self.kind = kind
        self.adapter: Any = from_secrets(cfg, {"clients": {"real": block}}, "real")

    def version(self) -> str:
        if self.kind == "qbittorrent":
            return str(self.adapter._c.app_version())
        return str(self.adapter._rpc("session-get").get("version"))

    def add_stopped(self, content: bytes, folder: Path) -> str | None:
        """Added STOPPED into ``folder``; the client's refusal (None: it took the torrent)."""
        if self.kind == "qbittorrent":
            answer = self.adapter._c.torrents_add(
                torrent_files=content,
                save_path=str(folder),
                is_stopped=True,
                use_auto_torrent_management=False,
                content_layout="Original",
            )
            return None if str(answer).strip() == "Ok." else f"qBittorrent answered {answer!r}"
        encoded = base64.b64encode(content).decode("ascii")
        answer = self.adapter._rpc("torrent-add", metainfo=encoded, paused=True, **{"download-dir": str(folder)})
        return None if isinstance(answer, dict) and "torrent-added" in answer else f"Transmission answered {answer!r}"

    def start(self, hashes: list[str]) -> None:
        if self.kind == "qbittorrent":
            self.adapter._c.torrents_start(torrent_hashes=hashes)
        else:
            self.adapter._rpc("torrent-start", ids=hashes)

    def recheck(self, hashes: list[str]) -> None:
        if self.kind == "qbittorrent":
            self.adapter._c.torrents_recheck(torrent_hashes=hashes)
        else:
            self.adapter._rpc("torrent-verify", ids=hashes)

    def remove(self, hashes: list[str]) -> None:
        if self.kind == "qbittorrent":
            self.adapter._c.torrents_delete(delete_files=False, torrent_hashes=hashes)
        else:
            self.adapter._rpc("torrent-remove", ids=hashes, **{"delete-local-data": False})

    def preallocate(self, on: bool | None) -> bool | None:
        """qBittorrent's "pre-allocate all files" set to ``on``; its previous value."""
        if self.kind != "qbittorrent" or on is None:
            return None
        previous = bool(self.adapter._c.app_preferences().get("preallocate_all"))
        self.adapter._c.app_set_preferences({"preallocate_all": on})
        return previous

    def info(self, infohash: str) -> dict[str, Any] | None:
        result: dict[str, Any] | None = self.adapter.inspect_torrent(infohash)
        return result


def _tow_accepts(content: bytes) -> str | None:
    """TOW's own parser on the case: None when it takes it, else its refusal."""
    from tow.torrent import parse_torrent_metadata

    try:
        parse_torrent_metadata(content)
    except ValueError as exc:
        return str(exc)
    return None


def _listed(info: dict[str, Any] | None) -> str | None:
    rows = (info or {}).get("files") or []
    return str(rows[0].get("name")).replace("\\", "/") if rows else None


def _checking(info: dict[str, Any] | None) -> bool:
    return str((info or {}).get("state") or "").casefold().startswith(("checking", "allocating", "moving"))


def _read_back(client: Client, hashes: dict[int, str]) -> dict[int, bool]:
    """After a check of the data: which cases' file the client found complete."""
    client.recheck(list(hashes.values()))
    started = time.monotonic()
    while True:
        infos = {index: client.info(h) for index, h in hashes.items()}
        done = {
            index: float(((info or {}).get("files") or [{}])[0].get("progress") or 0) >= 1
            for index, info in infos.items()
        }
        settled = time.monotonic() - started > 5 and not any(_checking(info) for info in infos.values())
        if all(done.values()) or settled or time.monotonic() - started > WAIT_SEC:
            return done
        time.sleep(1)


def _record(client: Client, base: Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    built = [torrent_of(index) for index in range(len(CASES))]
    contents = [content for content, _infohash in built]
    hashes = {index: infohash for index, (_content, infohash) in enumerate(built)}
    for index, content in enumerate(contents):
        cases.append({"torrent": "/".join((folder_of(index), *CASES[index])), "tow_refuses": _tow_accepts(content)})
    added: set[str] = set()
    previous = client.preallocate(True)
    try:
        # 1. Listed, added STOPPED into an empty folder; 2. created, once started.
        for index, content in enumerate(contents):
            try:
                refused = client.add_stopped(content, base / "listed" / str(index))
            except Exception as exc:  # noqa: BLE001 - a refusal is the observation
                refused = f"{type(exc).__name__}: {exc}"
            if refused is None:
                added.add(hashes[index])
            else:
                cases[index]["add_error"] = refused
        deadline = time.monotonic() + WAIT_SEC
        while time.monotonic() < deadline and any(
            client.info(hashes[i]) is None for i in range(len(CASES)) if hashes[i] in added
        ):
            time.sleep(1)
        for index in range(len(CASES)):
            cases[index]["listed"] = _listed(client.info(hashes[index])) if hashes[index] in added else None
        client.start(sorted(added))
        time.sleep(10)  # no peer can come: only what the client makes by itself
        for index in range(len(CASES)):
            cases[index]["created"] = on_disk(base / "listed" / str(index))
        client.remove(sorted(added))
        added.clear()
        time.sleep(2)
        # 3. Read back: the bytes under the torrent's own spelling, then under the listed one.
        for spelling in ("torrent", "listed"):
            wanted: dict[int, str] = {}
            for index, content in enumerate(contents):
                case = cases[index]
                listed = case.get("listed")
                if not isinstance(listed, str) or case.get("reads_torrent_spelling"):
                    continue
                if spelling == "listed" and listed == case["torrent"]:
                    case["reads_listed_spelling"] = case.get("reads_torrent_spelling")
                    continue
                folder = base / spelling / str(index)
                parts = (case["torrent"] if spelling == "torrent" else listed).split("/")[1:]
                if (refused := write_spelling(folder, index, parts)) is not None:
                    case[f"write_{spelling}_error"] = refused
                    case[f"reads_{spelling}_spelling"] = None
                    continue
                if (refused := client.add_stopped(content, folder)) is not None:
                    case[f"add_{spelling}_error"] = refused
                    continue
                added.add(hashes[index])
                wanted[index] = hashes[index]
            if wanted:
                found = _read_back(client, wanted)
                for index, complete in found.items():
                    cases[index][f"reads_{spelling}_spelling"] = complete
                    cases[index][f"on_disk_{spelling}"] = on_disk(base / spelling / str(index))
                client.remove(sorted(wanted.values()))
                added.difference_update(wanted.values())
                time.sleep(2)
    finally:
        with suppress(Exception):
            if added:
                client.remove(sorted(added))
        with suppress(Exception):
            client.preallocate(previous)
    return {"client": client.kind, "version": client.version(), "os": _os(), "platform": sys.platform, "cases": cases}


def test_record_the_spellings_of_reserved_names(tmp_path):
    out = os.environ.get("TOW_REAL_CLIENT_NAMES_OUT", "")
    if not out:
        pytest.fail("TOW_REAL_CLIENT_NAMES_OUT names the folder of the records")
    kinds = [kind for kind in ("qbittorrent", "transmission") if os.environ.get(f"TOW_REAL_{kind.upper()}")]
    assert kinds, "no client in the environment (TOW_REAL_QBITTORRENT, TOW_REAL_TRANSMISSION)"
    Path(out).mkdir(parents=True, exist_ok=True)
    for kind in kinds:
        record = _record(Client(kind), tmp_path / kind)
        target = Path(out) / f"{kind}-{record['os']}.json"
        target.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        taken = [case for case in record["cases"] if isinstance(case.get("listed"), str)]
        print(f"{kind} {record['version']} on {record['os']}: {len(taken)} of {len(CASES)} taken; {target}")
        for case in record["cases"]:
            print(json.dumps(case))  # ASCII: a Windows console may not take every name
        assert taken, f"{kind} took none of the torrents: {record['cases']}"


@pytest.mark.real_system("TOW_REAL_CLIENT_NAMES")
def test_tow_keys_agree_with_the_recorded_spellings():
    folder = Path(os.environ.get("TOW_REAL_CLIENT_NAMES_DIR", ""))
    records = sorted(folder.glob("*.json")) if str(folder) not in ("", ".") else []
    assert records, f"TOW_REAL_CLIENT_NAMES=1 needs the records in TOW_REAL_CLIENT_NAMES_DIR ({folder})"
    problems = []
    for path in records:
        problems.extend(mismatches(json.loads(path.read_text(encoding="utf-8"))))
    assert not problems, "TOW's keys disagree with the recorded client spellings:\n" + "\n".join(problems)
