"""Capacity evidence uses allocated bytes, not sparse files' logical length."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tow import platform
from tow.check import space


@pytest.mark.parametrize("system", ["linux", "macos"])
@pytest.mark.parametrize(("blocks", "expected"), [(0, 0), (4, 2048), (100, 4096), (None, None), (-1, None)])
def test_posix_allocation_uses_512_byte_blocks(system, blocks, expected):
    info = SimpleNamespace(st_mode=stat.S_IFREG, st_size=4096, st_blocks=blocks)
    with platform.use(platform.backend_for(system)):
        assert platform.allocated_file_bytes(info) == expected


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        (0, 4096),
        (stat.FILE_ATTRIBUTE_ARCHIVE, 4096),
        (stat.FILE_ATTRIBUTE_SPARSE_FILE, None),
        (stat.FILE_ATTRIBUTE_COMPRESSED, None),
        (stat.FILE_ATTRIBUTE_REPARSE_POINT, None),
        (None, None),
    ],
)
def test_windows_sparse_compressed_or_unknown_allocation_is_not_full_size(attributes, expected):
    info = SimpleNamespace(st_mode=stat.S_IFREG, st_size=4096)
    if attributes is not None:
        info.st_file_attributes = attributes
    with platform.use(platform.backend_for("windows")):
        assert platform.allocated_file_bytes(info) == expected


@pytest.mark.parametrize("mode", [stat.S_IFDIR, stat.S_IFIFO, stat.S_IFLNK])
def test_nonregular_entries_supply_no_capacity_credit(mode):
    info = SimpleNamespace(st_mode=mode, st_size=4096, st_blocks=8)
    assert platform.allocated_file_bytes(info) is None


@pytest.mark.parametrize("system", ["windows", "linux", "macos"])
def test_sparse_file_does_not_release_a_waiting_torrent(system, monkeypatch, tmp_path):
    destination = str(tmp_path)
    size = 10 * 1024**3
    path = tmp_path / "synthetic.mkv"
    real_lstat = Path.lstat

    def lstat(entry, *args, **kwargs):
        if entry == path:
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_size=size,
                st_blocks=0,
                st_file_attributes=stat.FILE_ATTRIBUTE_SPARSE_FILE,
            )
        return real_lstat(entry, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(space.shutil, "disk_usage", lambda _path: SimpleNamespace(free=100 * 1024**2))
    with platform.use(platform.backend_for(system)):
        shortfall = space.measure(destination, [((path,), size, 0)])
        assert shortfall is not None
        assert shortfall.needed == size
        # A client's actual completed-byte report remains useful even for sparse files.
        assert space.measure(destination, [((path,), size, size)]) is None


def test_a_failed_allocation_probe_never_credits_logical_size(monkeypatch, tmp_path):
    def refused(_path):
        raise PermissionError("synthetic refusal")

    monkeypatch.setattr(Path, "lstat", refused)
    assert space._on_disk([tmp_path / "synthetic.mkv"], 10 * 1024**3) == 0


def test_a_fully_allocated_alternative_is_reusable(monkeypatch, tmp_path):
    def lstat(path):
        blocks = 0 if path.name == "sparse.mkv" else 8
        return SimpleNamespace(st_mode=stat.S_IFREG, st_size=4096, st_blocks=blocks)

    monkeypatch.setattr(Path, "lstat", lstat)
    with platform.use(platform.backend_for("linux")):
        assert space._on_disk([tmp_path / "sparse.mkv", tmp_path / "complete.mkv"], 4096) == 4096
