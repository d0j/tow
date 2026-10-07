"""THIRD-PARTY-NOTICES.md names every package the Windows zip carries.

The zip's runtime\\cache holds the runtime packages of uv.lock for Windows (scripts/build-bundle.py
runs `uv sync --frozen --no-dev` there). They are read here from uv.lock - the project's
dependencies with their extras, their markers evaluated for 64-bit Windows and CPython - so a
dependency added or dropped in the lock fails this test until the notice says so."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = {
    "sys_platform": "win32",
    "platform_system": "Windows",
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "python_version": "3.14",
    "python_full_version": "3.14.0",
}


def _bundled_packages() -> set[str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    seen: set[tuple[str, str]] = set()
    names: set[str] = set()
    todo: list[tuple[str, str]] = [("tow", "")]
    while todo:
        name, extra = todo.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        names.add(name)
        package = packages[name]
        entries = package.get("optional-dependencies", {}).get(extra, []) if extra else package.get("dependencies", [])
        for entry in entries:
            marker = entry.get("marker")
            if marker and not Marker(marker).evaluate({**WINDOWS, "extra": ""}):
                continue
            todo.append((entry["name"], ""))
            todo.extend((entry["name"], wanted) for wanted in entry.get("extra", []))
    return names - {"tow"}


def _listed_packages() -> dict[str, str]:
    text = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    section = text.split("## Windows zip", 1)[1]
    return dict(re.findall(r"^\| ([a-z0-9][a-z0-9.-]*) \| ([^|]+?) \|$", section, re.MULTILINE))


def test_the_notice_lists_every_package_of_the_windows_zip_with_a_license():
    bundled = _bundled_packages()
    listed = _listed_packages()
    assert {"regex", "websockets", "httpcore", "python-multipart", "httptools", "watchfiles"} <= bundled
    assert "uvloop" not in bundled  # not on Windows
    assert set(listed) == bundled
    assert all(license_ for license_ in listed.values())


def test_the_notice_names_uv_and_cpython_of_the_zip():
    section = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8").split("## Windows zip", 1)[1]
    assert "runtime\\bin\\uv.exe" in section
    assert "MIT or Apache-2.0" in section
    assert "Python Software Foundation License" in section
