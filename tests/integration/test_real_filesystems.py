"""Real file systems for a save folder's volume (C03) and an exclusive publication (C04).

Skipped unless TOW_REAL_FILESYSTEMS=1, which only .github/workflows/real.yml sets: on Ubuntu a
tmpfs, a loop-mounted vfat and exFAT image (the kernel's drivers) and the same exFAT through FUSE
(exfat-fuse); on macOS exFAT and FAT disk images (hdiutil). Each leg (``filesystems.leg``) is
mounted, unmounted and mounted again by the test itself, in a folder of the test's own:

- C03: the volume TOW records for a folder there (``tow.folders.volume_at``) is the file
  system's own UUID (udev's /dev/disk/by-uuid on Linux, the volume UUID on macOS); the folder is
  seen on it and its free space counts. Unmounted - the folder gone, or its empty mount point left
  on the disk below - the folder is not seen on the recorded volume and its space is unknown
  (never the disk below's); mounted again it is seen again (the identity is stable across a
  remount); another file system at the same mount point is not the recorded volume, and neither
  is another of the SAME type (the leg's twin image, made with another UUID). A tmpfs has no
  identity: unknown, nothing is recorded.
- C04: ``atomic_create_bytes`` (what an export publishes through) without replacing: where the
  file system has hard links, by a link; where it has none, by the C library's no-replace rename
  (renameat2 RENAME_NOREPLACE, renamex_np RENAME_EXCL), which must exist on Linux's own vfat and
  exFAT; where it has neither (FUSE without rename flags, possibly macOS), it fails closed: an
  error and no file. A published name is never replaced, no temporary file is left, and a real
  ``tow export`` (export_bundle) is written there and read back.

What each leg did is printed (the job's log is the record).
"""

from __future__ import annotations

import base64
import errno
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from filesystems import LINUX_LEGS, MACOS_LEGS, Leg, leg, other, twin

pytestmark = [pytest.mark.real_system("TOW_REAL_FILESYSTEMS"), pytest.mark.timeout(300)]

SYSTEM = "macos" if sys.platform == "darwin" else "linux"
NAMES = (os.environ.get("TOW_REAL_FS") or " ".join(MACOS_LEGS if SYSTEM == "macos" else LINUX_LEGS)).split()


def _run(argv: tuple[str, ...]) -> None:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, f"{' '.join(argv)} exited with {done.returncode}: {done.stderr.strip()}"


class Mounted:
    """A leg at its mount point, mounted and unmounted on request; always unmounted at the end."""

    def __init__(self, name: str, base: Path) -> None:
        images = Path(os.environ.get("TOW_REAL_FS_IMAGES", ""))
        self.point = base / f"{name}-mount"
        self.point.mkdir(parents=True, exist_ok=True)
        ids = (os.getuid(), os.getgid())  # type: ignore[attr-defined,unused-ignore]
        self.leg: Leg = leg(name, SYSTEM, images, self.point, *ids)
        self.other: Leg = other(SYSTEM, images, self.point, *ids, name)
        self.twin: Leg | None = twin(SYSTEM, images, self.point, *ids, name)
        self.up: Leg | None = None

    def mount(self, which: Leg | None = None) -> None:
        self.point.mkdir(parents=True, exist_ok=True)  # hdiutil may remove it on detach
        chosen = which or self.leg
        _run(chosen.mount)
        self.up = chosen
        if SYSTEM == "linux":  # udev names the new loop device's UUID in /dev/disk/by-uuid
            subprocess.run(("udevadm", "settle", "--timeout=10"), capture_output=True, timeout=30, check=False)

    def unmount(self) -> None:
        if self.up is not None:
            _run(self.up.unmount)
            self.up = None


@pytest.fixture(params=NAMES)
def mounted(request, tmp_path) -> Iterator[Mounted]:
    if not os.environ.get("TOW_REAL_FS_IMAGES"):
        pytest.fail("TOW_REAL_FILESYSTEMS=1 needs TOW_REAL_FS_IMAGES (the folder of the images)")
    one = Mounted(request.param, tmp_path)
    one.mount()
    try:
        yield one
    finally:
        one.unmount()


def _space_known(folder: Path, volume: str | None) -> bool:
    from tow.check import space

    known, _shortfall = space._measured(str(folder), [((), 1_000_000, 0)], volume=volume)
    return known


def test_the_recorded_volume_follows_the_mount(mounted):
    from tow import platform
    from tow.folders import seen_from_here, volume_at

    folder = mounted.point / "Show S01"
    folder.mkdir()
    (folder / "e01.mkv").write_bytes(b"x" * 1000)
    recorded = volume_at(str(folder))
    found = platform.volume_of(folder)
    assert found is not None
    print(f"{mounted.leg.name}: {recorded!r}, {found.kind}")
    if mounted.leg.name != "exfat-fuse":
        assert found.kind == "local"
    if mounted.leg.name == "tmpfs":  # no UUID: unknown, never "tmpfs <mount point>"
        assert recorded is None
        assert found.identity is None
        assert seen_from_here(str(folder))  # nothing recorded: as without a record
        return
    assert recorded == found.identity
    assert recorded is not None
    assert recorded.startswith(("uuid:", "partuuid:") if SYSTEM == "linux" else ("uuid:",)), recorded
    assert str(mounted.point) not in recorded  # the file system's own name, not its place
    assert seen_from_here(str(folder), volume=recorded)
    assert _space_known(folder, recorded)

    mounted.unmount()
    below = volume_at(str(folder))  # the empty mount point (or its parent) on the disk below
    print(f"{mounted.leg.name}: recorded {recorded!r}; unmounted, the folder is on {below!r}")
    assert below != recorded
    assert not folder.exists()
    assert not seen_from_here(str(folder), volume=recorded)
    assert not _space_known(folder, recorded)  # unknown: the disk below's space is never credited

    mounted.mount()
    assert volume_at(str(folder)) == recorded  # a remount keeps the identity
    assert seen_from_here(str(folder), volume=recorded)
    assert _space_known(folder, recorded)
    assert (folder / "e01.mkv").read_bytes() == b"x" * 1000

    mounted.unmount()
    mounted.mount(mounted.other)  # another file system at the very same mount point
    folder.mkdir(exist_ok=True)
    elsewhere = volume_at(str(folder))
    print(f"{mounted.leg.name}: {mounted.other.name} at the same mount point is {elsewhere!r}")
    assert elsewhere != recorded
    assert not seen_from_here(str(folder), volume=recorded)
    assert not _space_known(folder, recorded)

    assert mounted.twin is not None
    mounted.unmount()
    mounted.mount(mounted.twin)  # the SAME type at the same mount point: another disk of its kind
    folder.mkdir(exist_ok=True)
    (folder / "e01.mkv").write_bytes(b"y" * 1000)  # even the same name and size there
    same_type = volume_at(str(folder))
    print(f"{mounted.leg.name}: {mounted.twin.name} (the same type) at the same mount point is {same_type!r}")
    assert same_type != recorded
    assert not seen_from_here(str(folder), volume=recorded)
    assert not _space_known(folder, recorded)


def _hard_link_errno(folder: Path) -> int | None:
    source, target = folder / "link-source", folder / "link-target"
    source.write_bytes(b"probe")
    try:
        os.link(source, target)
    except OSError as exc:
        return exc.errno
    finally:
        for path in (source, target):
            path.unlink(missing_ok=True)
    return None


def _native_rename(folder: Path) -> int:
    from tow.platform import posix

    source, target = folder / "rename-source", folder / "rename-target"
    source.write_bytes(b"probe")
    try:
        return posix._rename_no_replace(str(source), str(target), SYSTEM)
    finally:
        for path in (source, target):
            path.unlink(missing_ok=True)


def _left(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def test_an_exclusive_publication_never_replaces_and_needs_no_hard_links(mounted, monkeypatch):
    from tow.bundle.archive import read_bundle
    from tow.bundle.export import export_bundle
    from tow.storage.atomic import atomic_create_bytes
    from tow.store import save_state

    folder = mounted.point / "exports"
    folder.mkdir()
    link = _hard_link_errno(folder)
    native = _native_rename(folder)
    said = f"{mounted.leg.name}: os.link {errno.errorcode.get(link or 0, 'works')}; no-replace rename "
    print(said + ("works" if native == 0 else errno.errorcode.get(native, str(native))))
    assert (link is None) is mounted.leg.hard_links, link
    if mounted.leg.native_rename:
        assert native == 0, errno.errorcode.get(native, native)
    publishes = link is None or native == 0

    target = folder / "tow-export.towx"
    if not publishes:
        try:
            atomic_create_bytes(target, b"first complete export")
        except OSError as refused:
            print(f"{mounted.leg.name}: fails closed ({refused!r})")
        else:
            pytest.fail("published although the file system has neither hard links nor the no-replace rename")
        assert _left(folder) == []  # no destination, no temporary file
        return
    atomic_create_bytes(target, b"first complete export")
    with pytest.raises(FileExistsError):
        atomic_create_bytes(target, b"second export")  # a second publisher never replaces it
    assert target.read_bytes() == b"first complete export"
    assert _left(folder) == ["tow-export.towx"]

    # `tow export` itself, published the same way, read back.
    monkeypatch.setenv("TOW_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    save_state({"topics": []})
    bundle = folder / "tow-real.towx"
    result = export_bundle(bundle, "correct horse battery staple")
    assert "state.json" in result["members"]
    assert "state.json" in read_bundle(bundle, "correct horse battery staple")["members"]
    assert _left(folder) == ["tow-export.towx", "tow-real.towx"]
    print(f"{mounted.leg.name}: published without replacing, an export read back")
