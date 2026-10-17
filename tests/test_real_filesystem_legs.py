"""The C03/C04 file system legs of real.yml (tests/integration/filesystems.py), without a runner.

Each leg is mounted by its own image (or a tmpfs) at the test's own mount point, owned by the
test's account, and unmounted by that mount point only; its file system type is one TOW calls
local, so the identity the test expects is the one TOW's mount table reading gives; the other
file system for the same mount point is of another type.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from integration.filesystems import LINUX_LEGS, MACOS_LEGS, leg, other

from tow.platform.posix import linux_volume

IMAGES, POINT = PurePosixPath("/runner/fs-images"), PurePosixPath("/tmp/pytest-of-runner/x/vfat-mount")


@pytest.mark.parametrize("name", LINUX_LEGS)
def test_each_linux_leg_mounts_its_own_image_for_this_account(name):
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
    # TOW reads the mount table: the leg's type is a local file system, named with its point.
    table = f"36 25 0:33 / {POINT} rw,relatime shared:1 - {one.fstype} /dev/loop3 rw\n"
    found = linux_volume(str(POINT / "Show"), table)
    assert found is not None
    assert (found.identity, found.kind) == (f"{one.fstype} {POINT}", "local")
    assert other("linux", IMAGES, POINT, 1001, 118, name).fstype != one.fstype


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


def test_an_unknown_leg_is_refused():
    with pytest.raises(ValueError, match="no Linux leg"):
        leg("ntfs", "linux", IMAGES, POINT, 1, 1)
    with pytest.raises(ValueError, match="no macOS leg"):
        leg("tmpfs", "macos", IMAGES, POINT, 1, 1)
