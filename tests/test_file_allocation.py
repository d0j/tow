"""Capacity evidence uses allocated bytes, not sparse files' logical length.

The Windows answer for a sparse or compressed file comes from the backend
(``allocated_bytes``: GetCompressedFileSizeW); tests inject it, so they run on every system.
"""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tow import platform
from tow.check import space
from tow.platform import windows
from tow.platform.windows import WindowsBackend

GIB = 1024**3


class AllocatingWindows(WindowsBackend):
    """Windows, with the file system's answer for a sparse or compressed file given by the test."""

    def __init__(self, allocated: int | Exception | None) -> None:
        self.allocated = allocated
        self.asked: list[Path] = []

    def allocated_bytes(self, path: Path) -> int | None:
        self.asked.append(path)
        if isinstance(self.allocated, Exception):
            raise self.allocated
        return self.allocated


def _windows_file(size: int, attributes: int | None) -> SimpleNamespace:
    info = SimpleNamespace(st_mode=stat.S_IFREG, st_size=size)
    if attributes is not None:
        info.st_file_attributes = attributes
    return info


@pytest.mark.parametrize("system", ["linux", "macos"])
@pytest.mark.parametrize(("blocks", "expected"), [(0, 0), (4, 2048), (100, 4096), (None, None), (-1, None)])
def test_posix_allocation_uses_512_byte_blocks(system, blocks, expected):
    info = SimpleNamespace(st_mode=stat.S_IFREG, st_size=4096, st_blocks=blocks)
    with platform.use(platform.backend_for(system)):
        assert platform.allocated_file_bytes(Path("synthetic.mkv"), info=info) == expected


@pytest.mark.parametrize(
    ("size", "attributes", "allocated", "expected"),
    [
        (4096, 0, None, 4096),  # an ordinary file reserves its size; the file system is not asked
        (4096, stat.FILE_ATTRIBUTE_ARCHIVE, None, 4096),
        (4096, stat.FILE_ATTRIBUTE_SPARSE_FILE, 0, 0),  # an empty sparse file
        (4096, stat.FILE_ATTRIBUTE_SPARSE_FILE, 2048, 2048),  # partly written
        (4096, stat.FILE_ATTRIBUTE_SPARSE_FILE, 4096, 4096),  # fully written
        (4096, stat.FILE_ATTRIBUTE_SPARSE_FILE, 8192, 4096),  # cluster rounding: never above its size
        (4096, stat.FILE_ATTRIBUTE_COMPRESSED, 1000, 1000),  # compressed: what it takes, a cautious credit
        (5 * GIB, stat.FILE_ATTRIBUTE_SPARSE_FILE, 5 * GIB - 4096, 5 * GIB - 4096),  # above 4 GiB
        (4096, stat.FILE_ATTRIBUTE_SPARSE_FILE, None, None),  # the file system did not say
        (4096, stat.FILE_ATTRIBUTE_SPARSE_FILE, OSError("synthetic refusal"), None),
        (4096, stat.FILE_ATTRIBUTE_SPARSE_FILE, -1, None),
        (4096, None, 4096, None),  # no attributes: unknown
    ],
)
def test_windows_allocation_of_sparse_and_compressed_files(size, attributes, allocated, expected):
    backend = AllocatingWindows(allocated)
    with platform.use(backend):
        assert platform.allocated_file_bytes(Path("synthetic.mkv"), info=_windows_file(size, attributes)) == expected
    sparse_or_compressed = stat.FILE_ATTRIBUTE_SPARSE_FILE | stat.FILE_ATTRIBUTE_COMPRESSED
    asks = attributes is not None and bool(attributes & sparse_or_compressed)
    assert backend.asked == ([Path("synthetic.mkv")] if asks else [])


@pytest.mark.parametrize(
    "attributes",
    [stat.FILE_ATTRIBUTE_REPARSE_POINT, stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_SPARSE_FILE],
)
def test_windows_reparse_points_are_never_credited_or_asked(attributes):
    backend = AllocatingWindows(4096)
    with platform.use(backend):
        assert platform.allocated_file_bytes(Path("synthetic.mkv"), info=_windows_file(4096, attributes)) is None
    assert backend.asked == []


@pytest.mark.parametrize("mode", [stat.S_IFDIR, stat.S_IFIFO, stat.S_IFLNK])
def test_nonregular_entries_supply_no_capacity_credit(mode):
    info = SimpleNamespace(st_mode=mode, st_size=4096, st_blocks=8)
    assert platform.allocated_file_bytes(Path("synthetic"), info=info) is None


@pytest.mark.parametrize(
    ("low", "high", "last_error", "expected"),
    [
        (4096, 0, 0, 4096),
        (16, 1, 0, 4 * GIB + 16),  # the high DWORD
        (0xFFFFFFFF, 0, 0, 0xFFFFFFFF),  # a legitimate low DWORD of all ones: no error was set
        (0xFFFFFFFF, 1, 0, 8 * GIB - 1),
        (0xFFFFFFFF, 0, 2, None),  # the failure value with an error (file not found)
        (0xFFFFFFFF, 0, 5, None),  # access denied
    ],
)
def test_windows_compressed_size_combines_both_dwords(low, high, last_error, expected):
    assert windows.file_allocation(low, high, last_error) == expected


@pytest.mark.skipif(platform.this_os() == "windows", reason="the call exists on Windows")
def test_the_windows_call_is_unknown_elsewhere(tmp_path):
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"x" * 10)
    assert windows.compressed_file_size(path) is None


@pytest.mark.skipif(platform.this_os() != "windows", reason="a real Windows call")
def test_the_windows_call_reads_an_ordinary_file_and_refuses_a_missing_one(tmp_path):
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"x" * 10)
    assert windows.compressed_file_size(path) == 10
    assert windows.compressed_file_size(tmp_path / "missing.bin") is None


@pytest.mark.parametrize(("system", "credit"), [("windows", 0), ("linux", 0), ("macos", 0), ("windows", 10 * GIB)])
def test_sparse_file_releases_a_waiting_torrent_only_by_its_allocation(system, credit, monkeypatch, tmp_path):
    destination = str(tmp_path)
    size = 10 * GIB
    path = tmp_path / "synthetic.mkv"
    real_lstat = Path.lstat

    def lstat(entry, *args, **kwargs):
        if entry == path:
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_size=size,
                st_blocks=credit // 512,
                st_file_attributes=stat.FILE_ATTRIBUTE_SPARSE_FILE,
            )
        return real_lstat(entry, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(space.shutil, "disk_usage", lambda _path: SimpleNamespace(free=100 * 1024**2))
    backend = AllocatingWindows(credit) if system == "windows" else platform.backend_for(system)
    with platform.use(backend):
        shortfall = space.measure(destination, [((path,), size, 0)])
        if credit:
            assert shortfall is None  # every byte is already written: nothing more is needed
        else:
            assert shortfall is not None
            assert shortfall.needed == size
            # A client's actual completed-byte report remains useful even for sparse files.
            assert space.measure(destination, [((path,), size, size)]) is None


def test_a_partly_written_sparse_file_on_windows_counts_what_it_holds(monkeypatch, tmp_path):
    path = tmp_path / "synthetic.mkv"
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _entry: SimpleNamespace(
            st_mode=stat.S_IFREG, st_size=10 * GIB, st_file_attributes=stat.FILE_ATTRIBUTE_SPARSE_FILE
        ),
    )
    with platform.use(AllocatingWindows(6 * GIB)):
        assert space._on_disk([path], 10 * GIB) == 6 * GIB


def test_a_failed_allocation_probe_never_credits_logical_size(monkeypatch, tmp_path):
    def refused(_path):
        raise PermissionError("synthetic refusal")

    monkeypatch.setattr(Path, "lstat", refused)
    assert space._on_disk([tmp_path / "synthetic.mkv"], 10 * GIB) == 0


def test_a_fully_allocated_alternative_is_reusable(monkeypatch, tmp_path):
    def lstat(path):
        blocks = 0 if path.name == "sparse.mkv" else 8
        return SimpleNamespace(st_mode=stat.S_IFREG, st_size=4096, st_blocks=blocks)

    monkeypatch.setattr(Path, "lstat", lstat)
    with platform.use(platform.backend_for("linux")):
        assert space._on_disk([tmp_path / "sparse.mkv", tmp_path / "complete.mkv"], 4096) == 4096
