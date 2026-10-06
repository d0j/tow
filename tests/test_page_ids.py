"""Site names and topic ids are parts of page addresses (/sites/<name>/delete,
/topics/<id>/pause): config.yaml and an imported bundle accept only [A-Za-z0-9_-]{1,64}, and
the pages encode them anyway."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tow.bundle import ExportImportError, _validate_config_schema, _validate_state_schema
from tow.config import ConfigError, validated


@pytest.mark.parametrize("name", ["../topics/1", "a/b", "a\\b", "..", "."])
def test_config_and_import_refuse_a_site_name_that_moves_a_page_address(name):
    raw = {"trackers": {name: {"fetch_hosts": ["https://tracker.example"]}}}
    with pytest.raises(ConfigError, match="site name"):
        validated(raw)
    with pytest.raises(ExportImportError, match="site name"):
        _validate_config_schema(raw)


@pytest.mark.parametrize("name", ["site name", "", "x" * 65, "сайт", 123, None])
def test_an_older_odd_site_name_still_loads_but_is_never_imported(name):
    raw = {"trackers": {name: {"fetch_hosts": ["https://tracker.example"]}}}
    assert name in validated(raw)["trackers"]  # legacy config.yaml keeps working
    with pytest.raises(ExportImportError, match=r"site name|non-string key"):
        _validate_config_schema(raw)


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
