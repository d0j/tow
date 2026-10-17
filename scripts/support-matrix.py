"""Which systems TOW's locked dependencies support: the evidence behind docs/PORTABLE.md §1b.

    uv run --frozen python scripts/support-matrix.py              # the table, from uv.lock alone
    uv run --frozen python scripts/support-matrix.py --python     # also the pinned uv's Python builds

For every target ``install.sh`` (or the Windows bundle) can choose, it walks the runtime
dependencies of ``uv.lock`` (no dev group; each marker evaluated for that system) and names the
packages without a wheel CPython 3.14 there can use: those ``uv sync`` builds from source, which
needs compilers (and Rust for cryptography) on the owner's machine. It also gives the oldest glibc,
musl or macOS the wheels ask for. ``--python`` asks the bundle's uv (offline) whether its pinned
Python manifest has a build of ``.python-version`` for each target.

A complete wheel set is necessary, not sufficient: a system is supported only once a fresh install
and a start ran there (the ``verified`` column of the document, from CI). ``tests/test_support_matrix.py``
keeps the document's wheel column and the installers' targets in step with this file.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "3.14"

# target -> (marker environment, wheel platform tags it takes, how the oldest version is named)
TARGETS: dict[str, tuple[dict[str, str], tuple[str, ...], str]] = {
    "windows-x86_64": ({"sys_platform": "win32", "os_name": "nt", "platform_machine": "AMD64"}, (r"win_amd64",), ""),
    "windows-aarch64": ({"sys_platform": "win32", "os_name": "nt", "platform_machine": "ARM64"}, (r"win_arm64",), ""),
    "linux-x86_64-gnu": ({"sys_platform": "linux", "platform_machine": "x86_64"}, (r"manylinux\w*_x86_64",), "glibc"),
    "linux-aarch64-gnu": (
        {"sys_platform": "linux", "platform_machine": "aarch64"},
        (r"manylinux\w*_aarch64",),
        "glibc",
    ),
    "linux-x86_64-musl": ({"sys_platform": "linux", "platform_machine": "x86_64"}, (r"musllinux\w*_x86_64",), "musl"),
    "linux-aarch64-musl": (
        {"sys_platform": "linux", "platform_machine": "aarch64"},
        (r"musllinux\w*_aarch64",),
        "musl",
    ),
    "linux-armv7-gnueabihf": (
        {"sys_platform": "linux", "platform_machine": "armv7l"},
        (r"manylinux\w*_armv7l",),
        "glibc",
    ),
    "linux-armv7-musleabihf": (
        {"sys_platform": "linux", "platform_machine": "armv7l"},
        (r"musllinux\w*_armv7l",),
        "musl",
    ),
    "macos-aarch64": (
        {"sys_platform": "darwin", "platform_machine": "arm64"},
        (r"macosx_\d+_\d+_(?:arm64|universal2)",),
        "macOS",
    ),
    "macos-x86_64": (
        {"sys_platform": "darwin", "platform_machine": "x86_64"},
        (r"macosx_\d+_\d+_(?:x86_64|intel|universal2)",),
        "macOS",
    ),
}
_BASE = {
    "python_version": PYTHON,
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "os_name": "posix",
    "extra": "",
}
_SYSTEMS = {"win32": "Windows", "linux": "Linux", "darwin": "Darwin"}


def _environment(target: str, python_full: str) -> dict[str, str]:
    marker, _tags, _floor = TARGETS[target]
    return {**_BASE, "python_full_version": python_full, "platform_system": _SYSTEMS[marker["sys_platform"]], **marker}


def runtime_packages(lock: dict, environment: dict[str, str]) -> list[dict]:
    """The packages ``uv sync --no-dev`` installs for ``environment``: tow's dependencies, their
    extras and their own, each marker evaluated (never the dev group)."""
    packages = {package["name"]: package for package in lock["package"]}
    seen: set[tuple[str, tuple[str, ...]]] = set()
    todo: list[tuple[str, tuple[str, ...]]] = [("tow", ())]
    while todo:
        name, extras = todo.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        package = packages[name]
        dependencies = list(package.get("dependencies", []))
        for extra in extras:
            dependencies += package.get("optional-dependencies", {}).get(extra, [])
        for dependency in dependencies:
            marker = dependency.get("marker")
            if marker is None or Marker(marker).evaluate(environment):
                todo.append((dependency["name"], tuple(sorted(dependency.get("extra", ())))))
    return [packages[name] for name in sorted({name for name, _extras in seen} - {"tow"})]


def _wheel_tags(url: str) -> tuple[list[str], list[str], list[str]]:
    name = url.rsplit("/", 1)[-1].removesuffix(".whl")
    python, abi, platform = name.split("-")[-3:]
    return python.split("."), abi.split("."), platform.split(".")


def usable(url: str, platforms: tuple[str, ...]) -> bool:
    """A wheel CPython 3.14 (not free-threaded) installs on a system with these platform tags."""
    pythons, abis, tags = _wheel_tags(url)
    if "any" not in tags and not any(re.fullmatch(pattern, tag) for pattern in platforms for tag in tags):
        return False
    minor = int(PYTHON.split(".")[1])
    if "none" in abis and any(python.startswith("py3") or python == f"cp3{minor}" for python in pythons):
        return True
    if f"cp3{minor}" in abis and f"cp3{minor}" in pythons:
        return True
    return "abi3" in abis and any(re.fullmatch(r"cp3(\d+)", python) and int(python[3:]) <= minor for python in pythons)


def _oldest(url: str) -> tuple[int, int]:
    """The oldest glibc, musl or macOS a wheel asks for ((0, 0): any)."""
    found: tuple[int, int] | None = None
    for tag in _wheel_tags(url)[2]:
        match = re.match(r"(?:many|musl)linux_(\d+)_(\d+)_|macosx_(\d+)_(\d+)_", tag)
        if match:
            numbers = [int(part) for part in match.groups() if part is not None]
            version = (numbers[0], numbers[1])
        elif tag.startswith("manylinux2014"):
            version = (2, 17)
        else:
            return (0, 0)
        found = version if found is None or version < found else found
    return found or (0, 0)


def matrix(lock_path: Path, python_full: str) -> dict[str, dict]:
    """Per target: the number of runtime packages, those without a usable wheel, and the oldest
    system version the wheels need (with the package that needs it)."""
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    result = {}
    for target, (_marker, platforms, floor_name) in TARGETS.items():
        packages = runtime_packages(lock, _environment(target, python_full))
        no_wheel, oldest, by = [], (0, 0), ""
        for package in packages:
            wheels = [wheel["url"] for wheel in package.get("wheels", []) if usable(wheel["url"], platforms)]
            if not wheels:
                no_wheel.append(package["name"])
                continue
            need = min(_oldest(url) for url in wheels)
            if need > oldest:
                oldest, by = need, package["name"]
        result[target] = {
            "packages": len(packages),
            "no_wheel": no_wheel,
            "oldest": f"{floor_name} {oldest[0]}.{oldest[1]} ({by})" if floor_name and by else "",
        }
    return result


def python_builds(uv: str, version: str) -> set[str]:
    """``uv python list`` of the pinned uv, offline: the targets with a download of ``version``
    in the manifest that uv carries (the one pyproject's python-downloads-json-url pins)."""
    done = subprocess.run(
        [uv, "python", "list", version, "--all-platforms", "--all-arches", "--only-downloads", "--offline"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    found = set()
    for line in done.stdout.splitlines():
        match = re.match(rf"cpython-{re.escape(version)}-(\S+?)(?:-none)?\s", line)
        if match:
            found.add(match.group(1))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The systems uv.lock's runtime wheels cover.")
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument("--python", metavar="UV", nargs="?", const="uv", help="also list the Python builds (this uv)")
    args = parser.parse_args(argv)
    python_full = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    builds = python_builds(args.python, python_full) if args.python else None
    print(f"CPython {python_full}, {args.lock.name}")
    for target, row in matrix(args.lock, python_full).items():
        build = "" if builds is None else ("python build: yes; " if target in builds else "python build: NO; ")
        wheels = ", ".join(row["no_wheel"]) or "-"
        print(f"{target}: {build}{row['packages']} packages; no wheel: {wheels}; oldest: {row['oldest'] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
