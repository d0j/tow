"""The file systems of the C03/C04 legs (test_real_filesystems.py): how each is mounted and what it holds.

real.yml makes the images (mkfs.vfat, mkfs.exfat on Linux; hdiutil on macOS) in
TOW_REAL_FS_IMAGES and names the legs in TOW_REAL_FS (space-separated); the test mounts, unmounts
and mounts them again itself with these commands. tests/test_real_filesystem_legs.py checks them
without a runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath


@dataclass(frozen=True)
class Leg:
    name: str
    fstype: str  # as /proc/self/mountinfo (Linux) or statfs (macOS) names it
    mount: tuple[str, ...]
    unmount: tuple[str, ...]
    # Hard links: True (tmpfs), False (FAT, exFAT); publication then needs the no-replace rename.
    hard_links: bool
    # The no-replace rename: True where the system must offer it (Linux's own vfat and exfat),
    # None where either answer is evidence (FUSE, macOS): published, or refused without a file.
    native_rename: bool | None


LINUX_LEGS = ("tmpfs", "vfat", "exfat", "exfat-fuse")
MACOS_LEGS = ("exfat", "msdos")


def leg(name: str, system: str, images: PurePath, mount_point: PurePath, uid: int, gid: int) -> Leg:
    """How leg ``name`` is mounted at ``mount_point`` on ``system`` ("linux" or "macos")."""
    point = str(mount_point)
    owner = f"uid={uid},gid={gid}"
    if system == "macos":
        if name not in MACOS_LEGS:
            raise ValueError(f"no macOS leg {name!r}")
        image = str(images / f"{name}.dmg")
        attach = ("hdiutil", "attach", "-nobrowse", "-noautoopen", "-mountpoint", point, image)
        return Leg(name, name, attach, ("hdiutil", "detach", point), False, None)
    unmount = ("sudo", "-n", "umount", point)
    if name == "tmpfs":
        mount = ("sudo", "-n", "mount", "-t", "tmpfs", "-o", f"size=32m,mode=0700,{owner}", "tow-tmpfs", point)
        return Leg(name, "tmpfs", mount, unmount, True, None)
    if name in ("vfat", "exfat"):
        image = str(images / f"{name}.img")
        mount = ("sudo", "-n", "mount", "-t", name, "-o", f"loop,{owner},umask=077", image, point)
        return Leg(name, name, mount, unmount, False, True)
    if name == "exfat-fuse":
        # FUSE mounted by root takes a block device: the image through a loop device of its own,
        # released by its image's name after the unmount.
        image = str(images / "exfat-fuse.img")
        script = 'dev=$(losetup --find --show "$1") && exec mount.exfat-fuse -o "$3" "$dev" "$2"'
        mount = ("sudo", "-n", "sh", "-c", script, "sh", image, point, f"allow_other,{owner},umask=077")
        release = 'umount "$1" && losetup -j "$2" | cut -d: -f1 | xargs -r -n 1 losetup -d'
        return Leg(name, "fuseblk", mount, ("sudo", "-n", "sh", "-c", release, "sh", point, image), False, None)
    raise ValueError(f"no Linux leg {name!r}")


def other(system: str, images: PurePath, mount_point: PurePath, uid: int, gid: int, name: str) -> Leg:
    """Another file system for the same mount point: its identity must differ from ``name``'s."""
    if system == "macos":
        return leg("msdos" if name == "exfat" else "exfat", system, images, mount_point, uid, gid)
    return leg("vfat" if name == "tmpfs" else "tmpfs", system, images, mount_point, uid, gid)
