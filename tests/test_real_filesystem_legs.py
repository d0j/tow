"""The C03/C04 file system legs of real.yml (tests/integration/filesystems.py), without a runner.

Each leg is mounted by its own image (or a tmpfs) at the test's own mount point, owned by the
test's account, and unmounted by that mount point only; its file system type is one TOW calls
local, and its identity is the UUID udev names its device by (a tmpfs has none); the other file
system for the same mount point is of another type, and its twin is the same type from another
image; each test mounts copies of the images of its own.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from integration.filesystems import LINUX_LEGS, MACOS_LEGS, leg, other, own_images, twin

from tow.platform import posix
from tow.platform.posix import linux_volume

IMAGES, POINT = PurePosixPath("/runner/fs-images"), PurePosixPath("/tmp/pytest-of-runner/x/vfat-mount")


@pytest.mark.parametrize("name", LINUX_LEGS)
def test_each_linux_leg_mounts_its_own_image_for_this_account(name, monkeypatch):
    one = leg(name, "linux", IMAGES, POINT, 1001, 118)
    image = str(IMAGES / f"{name}.img")
    assert one.mount[:2] == one.unmount[:2] == ("sudo", "-n")
    if name == "exfat-fuse":  # through a loop device of its own, released after the unmount
        assert one.mount[-3:] == (image, str(POINT), "allow_other,uid=1001,gid=118,umask=077")
        assert 'losetup --find --show "$1"' in one.mount[4]
        assert one.unmount[-2:] == (str(POINT), image)
        assert 'umount "$1" && losetup -j "$2"' in one.unmount[4]
    else:
        assert one.mount[-1] == str(POINT)
        assert one.unmount == ("sudo", "-n", "umount", str(POINT))
        assert "uid=1001,gid=118" in one.mount[one.mount.index("-o") + 1]
    if name != "tmpfs":
        assert image in one.mount  # an image file, never a device of the runner
        assert not one.hard_links
    # TOW reads the mount table: the leg's type is a local file system, named by its device's UUID.
    monkeypatch.setattr(posix, "_block_identities", lambda: {"7:3": "uuid:0A1B-2C3D"})
    monkeypatch.setattr(posix, "_device_number", {"/dev/loop3": "7:3"}.get)
    number, source = ("0:33", "tow-tmpfs") if name == "tmpfs" else ("7:3", "/dev/loop3")
    table = f"36 25 {number} / {POINT} rw,relatime shared:1 - {one.fstype} {source} rw\n"
    found = linux_volume(str(POINT / "Show"), table)
    assert found is not None
    assert (found.identity, found.kind) == (None if name == "tmpfs" else "uuid:0A1B-2C3D", "local")
    assert other("linux", IMAGES, POINT, 1001, 118, name).fstype != one.fstype
    same = twin("linux", IMAGES, POINT, 1001, 118, name)
    if name == "tmpfs":
        assert same is None
    else:  # the same type, mounted the same way at the same point, from its twin image
        assert same is not None
        assert (same.fstype, same.unmount[:3]) == (one.fstype, one.unmount[:3])
        assert str(IMAGES / f"{name}-twin.img") in same.mount
        assert image not in same.mount
        assert str(POINT) in same.mount


def test_the_kernel_drivers_must_offer_the_no_replace_rename_and_fuse_may_not():
    legs = {name: leg(name, "linux", IMAGES, POINT, 1, 1) for name in LINUX_LEGS}
    assert legs["tmpfs"].hard_links
    assert legs["vfat"].native_rename is legs["exfat"].native_rename is True
    assert legs["exfat-fuse"].native_rename is None  # either answer is evidence, never a replace
    assert legs["exfat-fuse"].fstype == "fuseblk"


@pytest.mark.parametrize("name", MACOS_LEGS)
def test_each_macos_leg_is_a_disk_image_attached_at_the_mount_point(name):
    one = leg(name, "macos", IMAGES, POINT, 501, 20)
    assert one.mount == (
        "hdiutil",
        "attach",
        "-nobrowse",
        "-noautoopen",
        "-mountpoint",
        str(POINT),
        str(IMAGES / f"{name}.dmg"),
    )
    assert one.unmount == ("hdiutil", "detach", str(POINT))
    assert one.native_rename is None
    assert other("macos", IMAGES, POINT, 501, 20, name).fstype != one.fstype
    same = twin("macos", IMAGES, POINT, 501, 20, name)
    assert same is not None
    assert same.fstype == one.fstype
    assert same.mount == (*one.mount[:-1], str(IMAGES / f"{name}-twin.dmg"))


@pytest.mark.parametrize(
    ("system", "name"), [*(("linux", name) for name in LINUX_LEGS), *(("macos", name) for name in MACOS_LEGS)]
)
def test_each_leg_mounts_copies_of_the_images_of_its_own(system, name, tmp_path):
    # macOS exFAT's other file system is the FAT leg's own image: the folder its test made there
    # was already in the FAT image when the FAT leg's test made it (FileExistsError).
    suffix = ".dmg" if system == "macos" else ".img"
    source = tmp_path / "fs-images"
    source.mkdir()
    for leg_name in MACOS_LEGS if system == "macos" else LINUX_LEGS:
        for stem in (leg_name, leg_name + "-twin"):
            (source / f"{stem}{suffix}").write_bytes(f"made by real.yml: {stem}".encode())
    own = own_images(name, system, source, tmp_path / "test" / "images")
    point = tmp_path / "test" / f"{name}-mount"
    legs = (
        leg(name, system, own, point, 1, 1),
        other(system, own, point, 1, 1, name),
        twin(system, own, point, 1, 1, name),
    )
    mounted = [Path(arg) for one in legs if one for arg in one.mount if arg.endswith(suffix)]
    if system == "macos":  # its own, the other type's, its twin
        expected = {name, "msdos" if name == "exfat" else "exfat", name + "-twin"}
    else:  # the other file system is a tmpfs, or a vfat for the tmpfs leg
        expected = {"vfat"} if name == "tmpfs" else {name, name + "-twin"}
    assert {image.stem for image in mounted} == expected
    for image in mounted:  # every image the test mounts is its own copy, as real.yml made it
        assert image.parent == own
        assert image.read_bytes() == (source / image.name).read_bytes()
        image.write_bytes(b"Show S01")  # what the test writes there
    for image in source.iterdir():
        assert image.read_bytes() == f"made by real.yml: {image.stem}".encode()


def test_an_unknown_leg_is_refused():
    with pytest.raises(ValueError, match="no Linux leg"):
        leg("ntfs", "linux", IMAGES, POINT, 1, 1)
    with pytest.raises(ValueError, match="no macOS leg"):
        leg("tmpfs", "macos", IMAGES, POINT, 1, 1)
