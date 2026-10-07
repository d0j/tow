"""What TOW knows about particular sites lives in one module per site (tow.trackers.presets)."""

from __future__ import annotations

import importlib
import pkgutil
import sys
import types
from pathlib import Path

import pytest
import yaml

import tow.trackers.presets as package
from tow import guess, i18n, notify, title
from tow.check.topic import _download_limited
from tow.trackers import GenericHttpTracker, presets
from tow.trackers.presets import SitePreset
from tow.web.views import _search_href

ROOT = Path(__file__).resolve().parents[1]


def test_every_site_module_is_discovered():
    modules = {info.name for info in pkgutil.iter_modules(package.__path__) if not info.name.startswith("_")}
    assert set(presets.presets()) == modules
    assert not presets.SKIPPED
    assert {"rutor", "kinozal", "nnmclub", "rutracker", "tapochek", "unionpeer", "fast_torrent"} <= modules


@pytest.mark.parametrize("name", sorted(presets.presets()))
def test_a_site_module_names_itself(name):
    preset = importlib.import_module(f"tow.trackers.presets.{name}").PRESET
    assert isinstance(preset, SitePreset)
    assert preset.name == name


def test_a_config_without_sites_watches_every_known_site():
    from tow.config import load_config, save_config
    from tow.paths import config_path

    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert "trackers" not in example  # the presets give them
    with_settings = {name: dict(preset.spec) for name, preset in presets.presets().items() if preset.spec}
    assert set(with_settings) >= {"kinozal", "nnmclub", "rutor", "rutracker", "tapochek", "unionpeer"}
    cfg = load_config()
    assert cfg["trackers"] == with_settings
    save_config(cfg)  # unchanged sites are not written out...
    assert "trackers" not in yaml.safe_load(config_path().read_text(encoding="utf-8"))
    del cfg["trackers"]["rutor"]
    save_config(cfg)  # ...a change writes the whole list, which is then the list
    assert set(load_config()["trackers"]) == set(with_settings) - {"rutor"}
    for preset in presets.presets().values():
        assert preset.browser_login == bool(preset.spec.get("browser_auth")), preset.name


@pytest.mark.parametrize("name", sorted(presets.presets()))
def test_a_site_text_exists_in_every_language(name):
    key = presets.browser_login_hint(name)
    for code in i18n.codes():
        assert not key or i18n.has(key, code), (code, key)


def test_presets_are_defaults_and_the_site_settings_win():
    kinozal = GenericHttpTracker("kinozal", {"url_regex": r"^https?://k/(\d+)"})
    assert kinozal.spec["download_limit"] is True
    assert kinozal.spec["search_path"] == "/browse.php?s={q}"
    own = GenericHttpTracker("kinozal", {"url_regex": r"^https?://k/(\d+)", "download_limit": False})
    assert own.spec["download_limit"] is False
    # A browser login is the site's own setting: never forced on a site configured without it.
    nnm = GenericHttpTracker("nnmclub", {"url_regex": r"^https?://n/(\d+)"})
    assert "browser_auth" not in nnm.spec
    spec = {"url_regex": r"^https?://x/(\d+)"}
    assert GenericHttpTracker("my_site", spec).spec is spec  # an unknown site is left as configured


def test_the_scattered_site_knowledge_comes_from_the_presets():
    assert notify._tracker_text("rutracker") == "RuTracker"
    assert notify._tracker_text("kinozal") == "Kinozal"
    assert notify._tracker_text("nnmclub") == "Nnmclub"  # no preset name: as before
    assert _download_limited(types.SimpleNamespace(name="kinozal", spec={})) is True
    assert _download_limited(types.SimpleNamespace(name="rutor", spec={})) is False
    rutor = GenericHttpTracker("rutor", {"url_regex": r"^https?://r/(\d+)", "fetch_hosts": ["http://rutor.info"]})
    assert _search_href(rutor, {}, "Show") == "http://rutor.info/search/0/0/100/0/Show"
    assert guess.canon_watch_url("http://d.rutor.info/download/123") == "http://rutor.info/torrent/123"
    assert title._drop_site_bits("Show [S01] :: NNM-Club") == "Show [S01]"


def test_a_browser_login_hint_is_the_sites_own_or_the_plain_one(monkeypatch):
    from tow.browser_auth import _sign_in_hint

    monkeypatch.setattr(i18n, "current", lambda: "en")
    assert "NNMClub" in _sign_in_hint("nnmclub")
    assert "my_site" in _sign_in_hint("my_site")


@pytest.fixture
def new_site(monkeypatch):
    """A site added as one module: nothing else changes."""
    module = types.ModuleType("tow.trackers.presets.zz_example")

    def guess_rule(parts):
        if parts.host != "zz.example":
            return None
        return parts.spec(name="zz_example", url_regex=parts.rx(r"/t/(\d+)"), download_path="/get/{id}")

    module.PRESET = SitePreset(
        name="zz_example",
        label="ZZ Example",
        brands=("zz-?example",),
        search_path="/find?q={q}",
        daily_limit=True,
        guess=guess_rule,
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    real = pkgutil.iter_modules

    def with_new(path):
        yield from real(path)
        if list(path) == list(package.__path__):
            yield pkgutil.ModuleInfo(None, "zz_example", False)

    monkeypatch.setattr(pkgutil, "iter_modules", with_new)
    presets._discover.cache_clear()
    title._site_words.cache_clear()
    yield module.PRESET
    monkeypatch.undo()
    presets._discover.cache_clear()
    title._site_words.cache_clear()


def test_a_new_site_is_one_module(new_site):
    assert presets.get("zz_example") is new_site
    assert notify._tracker_text("zz_example") == "ZZ Example"
    assert _download_limited(types.SimpleNamespace(name="zz_example", spec={})) is True
    tracker = GenericHttpTracker("zz_example", {"url_regex": r"^https?://zz/(\d+)", "fetch_hosts": ["https://zz"]})
    assert _search_href(tracker, {}, "a b") == "https://zz/find?q=a%20b"
    spec = guess.guess_from_url("https://zz.example/t/12345")
    assert spec["name"] == "zz_example"
    assert spec["download_path"] == "/get/{id}"
    assert guess._name_from_host("mirror.zz-example.net") == "zz_example"
    assert title._drop_site_bits("Show :: zz-example") == "Show"


def test_a_broken_site_module_is_skipped_with_its_reason(monkeypatch):
    broken = types.ModuleType("tow.trackers.presets.zz_broken")  # no PRESET
    monkeypatch.setitem(sys.modules, broken.__name__, broken)
    real = pkgutil.iter_modules

    def with_broken(path):
        yield from real(path)
        yield pkgutil.ModuleInfo(None, "zz_broken", False)

    monkeypatch.setattr(pkgutil, "iter_modules", with_broken)
    presets._discover.cache_clear()
    try:
        assert "zz_broken" not in presets.presets()
        assert "rutor" in presets.presets()
        assert [name for name, _reason in presets.SKIPPED] == ["zz_broken"]
    finally:
        monkeypatch.undo()
        presets._discover.cache_clear()
