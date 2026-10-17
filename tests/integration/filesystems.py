"""The file systems of the C03/C04 legs (test_real_filesystems.py): how each is mounted and what it holds.

real.yml makes the images (mkfs.vfat, mkfs.exfat on Linux; hdiutil on macOS) in
TOW_REAL_FS_IMAGES, each with a twin of the same type and another UUID (``<leg>-twin``), and names
the legs in TOW_REAL_FS (space-separated); each test copies the images it mounts into a folder of
its own (``own_images``) and mounts, unmounts and mounts them again itself with these commands.
tests/test_real_filesystem_legs.py checks them without a runner.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePath


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


# The suffix of a leg's twin image: the same file system type made again, another UUID.
TWIN = "-twin"


def leg(name: str, system: str, images: PurePath, mount_point: PurePath, uid: int, gid: int, *, image: str = "") -> Leg:
    """How leg ``name`` is mounted at ``mount_point`` on ``system`` ("linux" or "macos");
    ``image``: the name of its image when not the leg's own (its twin's)."""
    point = str(mount_point)
    owner = f"uid={uid},gid={gid}"
    stem = image or name
    if system == "macos":
        if name not in MACOS_LEGS:
            raise ValueError(f"no macOS leg {name!r}")
        disk = str(images / f"{stem}.dmg")
        attach = ("hdiutil", "attach", "-nobrowse", "-noautoopen", "-mountpoint", point, disk)
        return Leg(stem, name, attach, ("hdiutil", "detach", point), False, None)
    unmount = ("sudo", "-n", "umount", point)
    if name == "tmpfs":
        mount = ("sudo", "-n", "mount", "-t", "tmpfs", "-o", f"size=32m,mode=0700,{owner}", "tow-tmpfs", point)
        return Leg(name, "tmpfs", mount, unmount, True, None)
    if name in ("vfat", "exfat"):
        disk = str(images / f"{stem}.img")
        mount = ("sudo", "-n", "mount", "-t", name, "-o", f"loop,{owner},umask=077", disk, point)
        return Leg(stem, name, mount, unmount, False, True)
    if name == "exfat-fuse":
        # FUSE mounted by root takes a block device: the image through a loop device of its own,
        # released by its image's name after the unmount.
        image = str(images / f"{stem}.img")
        script = 'dev=$(losetup --find --show "$1") && exec mount.exfat-fuse -o "$3" "$dev" "$2"'
        mount = ("sudo", "-n", "sh", "-c", script, "sh", image, point, f"allow_other,{owner},umask=077")
        release = 'umount "$1" && losetup -j "$2" | cut -d: -f1 | xargs -r -n 1 losetup -d'
        return Leg(stem, "fuseblk", mount, ("sudo", "-n", "sh", "-c", release, "sh", point, image), False, None)
    raise ValueError(f"no Linux leg {name!r}")


def other(system: str, images: PurePath, mount_point: PurePath, uid: int, gid: int, name: str) -> Leg:
    """Another file system for the same mount point: its identity must differ from ``name``'s."""
    if system == "macos":
        return leg("msdos" if name == "exfat" else "exfat", system, images, mount_point, uid, gid)
    return leg("vfat" if name == "tmpfs" else "tmpfs", system, images, mount_point, uid, gid)


def twin(system: str, images: PurePath, mount_point: PurePath, uid: int, gid: int, name: str) -> Leg | None:
    """The same file system type as ``name`` from its twin image (made by real.yml with another
    UUID), mounted the same way at the same mount point: a disk replaced by another of its kind.
    None for a tmpfs (no identity to tell apart)."""
    if name == "tmpfs":
        return None
    return leg(name, system, images, mount_point, uid, gid, image=name + TWIN)


def own_images(name: str, system: str, source: Path, target: Path) -> Path:
    """Copies the images a test of leg ``name`` mounts (its own, the other file system's, its
    twin's) from ``source`` into ``target`` and returns ``target``: what one test writes there
    never reaches another's. The other file system of one leg is another leg's own image (on
    macOS exFAT's is the FAT one), so a folder made on it would be in that leg's image already."""
    target.mkdir(parents=True, exist_ok=True)
    mounted = (leg(name, system, source, target, 0, 0), other(system, source, target, 0, 0, name))
    for one in (*mounted, twin(system, source, target, 0, 0, name)):
        for image in one.mount if one else ():
            if Path(image).parent == source:
                shutil.copyfile(image, target / Path(image).name)
    return target
