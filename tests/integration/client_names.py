"""The names Windows reserves, as real clients spell them (B36): the cases, the record, the verdict.

``test_real_client_names.py`` gives each case below to a real client as its own private torrent
(synthetic bytes, an unreachable announce address, never a tracker) and records what the client
does with the name: the name it lists, the names it creates on disk when it starts the torrent,
and which spelling it reads back when the bytes are already there. ``mismatches`` compares a
record with TOW's keys (``tow.torrent.windows_path_key``); tests/test_client_name_records.py
checks it without a client.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

PIECE = 16384
# Path parts below the torrent's own folder: device names with and without an extension, the
# ones the save-folder rule refuses but no client is known to rename (COM0, LPT0, superscript
# digits, CONIN$/CONOUT$), trailing dots and spaces, a character Windows forbids, folders.
CASES: tuple[tuple[str, ...], ...] = (
    ("CON",),
    ("con",),
    ("aux.txt",),
    ("AUX",),
    ("NUL.tar.gz",),
    ("PRN",),
    ("COM1",),
    ("LPT9.log",),
    ("CLOCK$",),
    ("COM0",),
    ("LPT0",),
    ("COM¹",),
    ("LPT²",),
    ("COM³.txt",),
    ("CONIN$",),
    ("CONOUT$",),
    ("trailing dot.",),
    ("trailing space ",),
    ("aux.",),
    ("CON .txt",),
    ("Ep?.mkv",),
    ("AUX", "inner.bin"),
    ("COM0", "inner.bin"),
    ("folder.", "inner.bin"),
)


def folder_of(index: int) -> str:
    """The torrent's own (plain) folder name for case ``index``."""
    return f"tow-names-{index:02d}"


def content_of(index: int) -> bytes:
    """Synthetic bytes of the one file of case ``index``."""
    return b"".join(hashlib.sha256(f"tow-names/{index}/{n}".encode()).digest() for n in range(40))[:1200]


def torrent_of(index: int) -> tuple[bytes, str]:
    """Case ``index`` as a private v1 torrent with the real piece hash of its synthetic file, and
    its info hash (lower case), computed here: TOW's parser may refuse the name."""
    from helpers import bencode, make_torrent

    body = content_of(index)
    info = {
        b"name": folder_of(index).encode(),
        b"piece length": PIECE,
        b"pieces": hashlib.sha1(body).digest(),
        b"private": 1,  # no DHT or peer exchange: the client talks to nobody about it
        b"files": [{b"length": len(body), b"path": [part.encode() for part in CASES[index]]}],
    }
    return make_torrent(info, announce="http://127.0.0.1:9/announce"), hashlib.sha1(bencode(info)).hexdigest()


def raw_path(path: Path) -> str:
    """``path`` as the file system stores it: on Windows through ``\\\\?\\``, which keeps device
    names and trailing dots and spaces as they are (a plain path would open the console for CON)."""
    text = os.path.abspath(path)
    return "\\\\?\\" + text if os.name == "nt" and not text.startswith("\\\\?\\") else text


def write_spelling(folder: Path, index: int, parts: tuple[str, ...] | list[str]) -> str | None:
    """The case's bytes at ``folder``/<its torrent folder>/``parts`` spelled exactly; the error
    when the file system refuses that spelling (None: written)."""
    target = os.path.join(raw_path(folder / folder_of(index)), *parts)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(content_of(index))
    except OSError as exc:
        return f"{type(exc).__name__}: {exc.strerror or exc}"
    return None


def on_disk(folder: Path) -> list[str]:
    """Every file below ``folder`` as the file system names it, relative, with "/"."""
    base = raw_path(folder)
    found = []
    for current, _folders, files in os.walk(base):
        below = current[len(base) :].strip(os.sep)  # never normalized: "dot." keeps its dot
        found.extend("/".join(part for part in (*below.split(os.sep), name) if part) for name in files)
    return sorted(found)


def remove_tree(folder: Path) -> None:
    """Remove ``folder`` with the spellings a plain path cannot name (Windows: CON, "dot.")."""
    base = raw_path(folder)
    for current, folders, files in os.walk(base, topdown=False):
        for name in files:
            os.remove(os.path.join(current, name))
        for name in folders:
            os.rmdir(os.path.join(current, name))
    if os.path.isdir(base):
        os.rmdir(base)


def mismatches(record: dict[str, Any]) -> list[str]:
    """Where TOW's keys disagree with what the client did, one line each (empty: they agree).

    TOW matches a torrent's file to the client's list by ``windows_path_key`` (adds, the overlap
    check, file selection), and looks for a file on disk under the name the client lists (space,
    progress). So for each name the client took: the key of the torrent's spelling must equal the
    key of the listed one, and the spelling the client used on disk - created, or read back - must
    be the listed one (case-insensitively where the file system is)."""
    from tow.torrent import windows_path_key

    problems = []
    fold = record.get("os") == "windows"
    for case in record.get("cases") or []:
        torrent, listed = str(case.get("torrent") or ""), case.get("listed")
        label = f"{record.get('client')} on {record.get('os')}: {torrent!r}"
        if not isinstance(listed, str):
            continue  # the client did not take the torrent: nothing TOW could match
        if windows_path_key(torrent) != windows_path_key(listed):
            problems.append(
                f"{label} listed as {listed!r}: TOW's key {windows_path_key(torrent)!r} != {windows_path_key(listed)!r}"
            )
        used = list(case.get("created") or [])
        if case.get("reads_torrent_spelling"):
            used.append(torrent)
        if case.get("reads_listed_spelling"):
            used.append(listed)
        for spelling in used:
            same = spelling.casefold() == listed.casefold() if fold else spelling == listed
            if not same:
                problems.append(
                    f"{label} is {spelling!r} on disk but listed as {listed!r}: TOW looks for the listed one"
                )
    return problems
