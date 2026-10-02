"""The complexity cap (ruff C901) and the functions still allowed above it.

The list below may only shrink: a function split under the cap loses its ``# noqa: C901``
and its line here. A new exception needs the owner's agreement, not a bigger number.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# file -> function: the only places allowed above the cap, each with its reason on the line.
ALLOWED = {
    ("tests/conftest.py", "_no_real_system_effects"),
}
CAP = 20

_NOQA = re.compile(r"#\s*noqa:[^#\n]*\bC901\b")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")


def _exceptions() -> list[tuple[str, str, str]]:
    # ruff reports C901 on the "def" line: a noqa anywhere else suppresses nothing (RUF100
    # flags it), so only "def" lines count.
    return [
        (path.relative_to(ROOT).as_posix(), match.group(1), line)
        for folder in ("src", "tests", "scripts")
        for path in sorted((ROOT / folder).rglob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if _NOQA.search(line) and (match := _DEF.match(line))
    ]


def test_the_cap_is_not_raised():
    config = tomllib.loads((ROOT / "ruff.toml").read_text(encoding="utf-8"))
    assert config["lint"]["mccabe"]["max-complexity"] == CAP
    assert "C90" in config["lint"]["extend-select"]


def test_only_listed_functions_exceed_the_cap():
    found = _exceptions()
    assert {(path, name) for path, name, _line in found} <= ALLOWED, found
    assert len(found) <= len(ALLOWED)


def test_every_blind_except_says_why():
    """A kept ``except Exception`` is a boundary that must never crash: it says which one."""
    for folder in ("src", "tests"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"\s*except\b.*#\s*noqa:\s*BLE001", line):
                    reason = line.split("BLE001", 1)[1].strip(" -")
                    assert len(reason) > 10, f"{path.relative_to(ROOT)}:{number} keeps a blind except without why"


def test_every_exception_says_why():
    for path, name, line in _exceptions():
        reason = line.split("C901", 1)[1]
        assert reason.strip(" -"), f"{path}:{name} has no reason after noqa: C901"
