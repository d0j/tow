"""The wheel carries every runtime file of src/tow (scripts/gate.ps1, step 'wheel contents').

Every file under src/tow is required except Python's own caches, so a new package, template,
asset or data file is checked the day it is added - no list to keep up to date.
"""

import pathlib
import sys
import zipfile

# Files the source tree may hold that are not part of TOW: Python's byte-code caches.
_CACHE_DIR = "__pycache__"
_CACHE_SUFFIXES = (".pyc", ".pyo")
# Sanity: the scan must see these (a wrong src argument would otherwise "pass" with nothing).
REQUIRED = (
    "tow/__init__.py",
    "tow/templates/base.html",
    "tow/static/app.js",
    "tow/locales/en.json",
)


def expected_files(src: pathlib.Path) -> list[str]:
    """Every runtime file of ``src`` (the tow package folder) as its wheel path."""
    return sorted(
        "tow/" + path.relative_to(src).as_posix()
        for path in src.rglob("*")
        if path.is_file() and _CACHE_DIR not in path.parts and path.suffix not in _CACHE_SUFFIXES
    )


def missing_files(wheel: pathlib.Path, src: pathlib.Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    expected = expected_files(src)
    missing = [name for name in expected if name not in names]
    missing += [name + " (not even in the source tree)" for name in REQUIRED if name not in expected]
    return missing


def main(argv: list[str]) -> int:
    wheel, src = pathlib.Path(argv[0]), pathlib.Path(argv[1])
    if missing := missing_files(wheel, src):
        print("wheel is missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"wheel contents: {len(expected_files(src))} runtime files present in {wheel.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
