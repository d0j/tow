"""docs/PORTABLE.md §1b, the dependency contract, against uv.lock and the installers.

The wheel column is recomputed from the lock (scripts/support-matrix.py); the rows are the
targets the installers can choose. The Python-build column and the verification column are
evidence recorded by hand (uv's manifest, CI runs): this test only keeps their rows present.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("tow_support_matrix", ROOT / "scripts" / "support-matrix.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


support = _load()
PYTHON = (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def _section() -> str:
    text = (ROOT / "docs" / "PORTABLE.md").read_text(encoding="utf-8")
    start = text.index("## 1b. Supported systems")
    return text[start : text.index("\n## ", start + 1)]


def _rows() -> dict[str, list[str]]:
    rows = {}
    for line in _section().splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| ") and cells[0] in support.TARGETS:
            rows[cells[0]] = cells
    return rows


@pytest.fixture(scope="module")
def computed():
    return support.matrix(ROOT / "uv.lock", PYTHON)


def test_every_target_has_one_row_and_the_contract_names_its_python():
    assert sorted(_rows()) == sorted(support.TARGETS)
    assert f"Python {PYTHON} build" in _section()


def test_the_wheel_column_is_what_uv_lock_says(computed):
    for target, cells in _rows().items():
        documented = [] if cells[2] == "none" else [name.strip() for name in cells[2].split(",")]
        assert documented == computed[target]["no_wheel"], target


def test_supported_rows_need_no_source_build_and_were_started(computed):
    """Wheels alone are no support: a supported row also names the smoke test that started it."""
    supported = {target for target, cells in _rows().items() if cells[4].startswith("supported")}
    assert supported == {"windows-x86_64", "linux-x86_64-gnu", "macos-aarch64"}
    for target in supported:
        cells = _rows()[target]
        assert computed[target]["no_wheel"] == [], target
        assert cells[1] == "yes", target
        assert "smoke" in cells[3], target


def test_the_oldest_systems_said_match_the_wheels(computed):
    assert computed["linux-x86_64-gnu"]["oldest"].startswith("glibc 2.17 ")
    assert computed["macos-aarch64"]["oldest"].startswith("macOS 11.0 ")
    assert computed["linux-x86_64-musl"]["oldest"].startswith("musl 1.2 ")
    section = _section()
    assert "glibc 2.17 or newer" in section
    assert "macOS 11 or newer" in section


def test_install_sh_chooses_only_targets_of_the_contract():
    """install.sh's uv targets (with the libc it adds) are rows of the table; nothing else."""
    text = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    bases = set(re.findall(r"\) target=([a-z0-9_]+)-(?:unknown-linux|apple-darwin)", text))
    bases |= set(re.findall(r"^\s+target=([a-z0-9_]+)-apple-darwin", text, re.MULTILINE))
    assert bases == {"x86_64", "aarch64", "armv7"}
    rows = set(_rows())
    for base in ("x86_64", "aarch64"):
        assert {f"linux-{base}-gnu", f"linux-{base}-musl", f"macos-{base}"} <= rows
    assert {"linux-armv7-gnueabihf", "linux-armv7-musleabihf"} <= rows
    assert '*) die "this computer ($system $arch) is not supported"' in text


def test_usable_wheels_follow_the_tags():
    def url(name: str) -> str:
        return f"https://files.example/{name}"

    linux = support.TARGETS["linux-x86_64-gnu"][1]
    assert support.usable(url("a-1-py3-none-any.whl"), linux)
    assert support.usable(url("a-1-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"), linux)
    assert support.usable(url("a-1-cp311-abi3-manylinux_2_28_x86_64.whl"), linux)
    assert not support.usable(url("a-1-cp314-cp314t-manylinux_2_17_x86_64.whl"), linux)  # free-threaded
    assert not support.usable(url("a-1-cp313-cp313-manylinux_2_17_x86_64.whl"), linux)
    assert not support.usable(url("a-1-cp314-cp314-musllinux_1_2_x86_64.whl"), linux)
    assert not support.usable(url("a-1-cp315-abi3-manylinux_2_17_x86_64.whl"), linux)
