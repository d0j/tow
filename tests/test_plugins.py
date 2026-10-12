"""tow.plugins.scan: the module walk the torrent-client and messenger registries share."""

from __future__ import annotations

import logging
import sys

import pytest

from tow import plugins

PACKAGE = "zz_synthetic_plugins"
MODULES = {
    "__init__": "",
    "alpha": "KIND = 'alpha'\n",
    "beta": "KIND = 'beta'\n",
    "quiet": "VALUE = 1\n",  # no KIND: not a plugin, left out without a word
    "broken": "import zz_no_such_dependency\n",
    "incomplete": "KIND = 'incomplete'\n",  # load refuses it
    "helper": "raise RuntimeError('a helper module is never imported')\n",
    "_private": "raise RuntimeError('an underscore module is never imported')\n",
}


@pytest.fixture
def package(tmp_path, monkeypatch):
    folder = tmp_path / PACKAGE
    folder.mkdir()
    for name, source in MODULES.items():
        (folder / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield __import__(PACKAGE)
    for name in [name for name in sys.modules if name == PACKAGE or name.startswith(f"{PACKAGE}.")]:
        del sys.modules[name]


def _load(module):
    kind = getattr(module, "KIND", None)
    if kind is None:
        return None
    if kind == "incomplete":
        raise TypeError("not a plugin, missing send")
    return kind


def test_scan_finds_plugins_and_names_the_broken_ones(package, caplog):
    logger = logging.getLogger("tow.test_plugins")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        found, skipped = plugins.scan(package, helpers={"helper"}, load=_load, label="widget", logger=logger)
    assert sorted(found) == ["alpha", "beta"]
    assert sorted(skipped) == [
        ("broken", "ModuleNotFoundError: No module named 'zz_no_such_dependency'"),
        ("incomplete", "TypeError: not a plugin, missing send"),
    ]
    lines = sorted(record.getMessage() for record in caplog.records)
    assert lines == [
        "widget module broken skipped: ModuleNotFoundError: No module named 'zz_no_such_dependency'",
        "widget module incomplete skipped: TypeError: not a plugin, missing send",
    ]
    assert f"{PACKAGE}.helper" not in sys.modules
    assert f"{PACKAGE}._private" not in sys.modules
    assert f"{PACKAGE}.quiet" in sys.modules  # imported, then left out: load said it is no plugin


def test_scan_of_a_package_without_plugins_finds_nothing(package):
    everything = {name for name in MODULES if not name.startswith("_")}
    assert plugins.scan(package, helpers=everything, load=_load, label="widget", logger=logging.getLogger()) == (
        [],
        [],
    )
