"""Site names and topic ids are parts of page addresses (/sites/<name>/delete,
/topics/<id>/pause): a site name must not move such an address (no /, \\, . or ..), an imported
topic id is [A-Za-z0-9_-]{1,64}, and the pages encode both."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tow.bundle import ExportImportError, _validate_config_schema, _validate_state_schema
from tow.config import ConfigError, validated


@pytest.mark.parametrize("name", ["../topics/1", "a/b", "a\\b", "..", "."])
def test_config_and_import_refuse_a_site_name_that_moves_a_page_address(name):
    raw = {"trackers": {name: {"fetch_hosts": ["https://tracker.example"]}}}
    with pytest.raises(ConfigError) as caught:
        validated(raw)
    assert caught.value.code == "config_error.site_name"
    with pytest.raises(ExportImportError, match="site name"):
        _validate_config_schema(raw)


@pytest.mark.parametrize("name", ["site name", "", "x" * 65, "сайт", "kinozal.tv", 123, None])
def test_an_older_odd_site_name_loads_and_passes_its_own_backup(name):
    # Before: config.yaml loaded such a name, but the read-back of TOW's own restore point (and
    # with it the web update, tow export and the Monitorrent import) refused the same file.
    raw = {"trackers": {name: {"fetch_hosts": ["https://tracker.example"]}}}
    assert name in validated(raw)["trackers"]  # legacy config.yaml keeps working
    _validate_config_schema(raw)


def test_a_restore_point_of_a_config_with_a_legacy_site_name_is_made_and_checked(monkeypatch, tmp_path):
    import yaml
    from cryptography.fernet import Fernet

    from tow import restore_points
    from tow.store import save_state

    config = tmp_path / "config.yaml"
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(config))
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    site = {"url_regex": r"^https?://old\.example/t/(\d+)", "fetch_hosts": ["https://old.example"]}
    config.write_text(yaml.safe_dump({"trackers": {"kinozal.tv": site, 123: site}}), encoding="utf-8")
    save_state({"topics": [], "mirrors": {}})

    point = restore_points.create_restore_point()
    assert restore_points.check_restore_point(point["id"])["ok"] is True


@pytest.mark.parametrize("name", ["nnmclub", "Fast-Torrent_2", "x" * 64])
def test_plain_site_names_stay_accepted(name):
    raw = {"trackers": {name: {"fetch_hosts": ["https://tracker.example"]}}}
    assert name in validated(raw)["trackers"]
    _validate_config_schema(raw)


@pytest.mark.parametrize("topic_id", ["../sites/nnmclub", "a/b", "1 2", "", "x" * 65])
def test_import_refuses_a_topic_id_that_is_not_a_plain_id(topic_id):
    state = {"topics": [{"id": topic_id, "url": "https://tracker.example/t/1"}], "mirrors": {}}
    with pytest.raises(ExportImportError, match=r"topic\[0\]\.id"):
        _validate_state_schema(state)
    _validate_state_schema({"topics": [{"id": "a1b2c3d4e5f6"}], "mirrors": {}})


def test_page_addresses_encode_the_topic_id():
    from tow.store import save_state
    from tow.web import app

    topic = {"id": "odd id?x", "title": "Show A", "url": "http://rutor.info/torrent/1234567/show"}
    save_state({"topics": [topic], "mirrors": {}})
    page = TestClient(app).get("/").text
    assert 'action="/topics/odd%20id%3Fx/pause"' in page
    assert "/topics/odd id?x/" not in page
