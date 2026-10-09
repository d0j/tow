"""config.yaml broken while TOW runs: a page that says so, never a bare "Internal Server Error"."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from tow.paths import config_path
from tow.web import app

HTML = {"Accept": "text/html"}


@pytest.fixture
def broken_config(monkeypatch):
    import importlib

    monkeypatch.setattr(importlib.import_module("tow.web.app"), "_config_logged", [])
    path = config_path()
    good = path.read_text(encoding="utf-8")
    path.write_text(good + "trackers: [unclosed\n", encoding="utf-8")
    yield path
    path.write_text(good, encoding="utf-8")


def _local() -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def test_the_page_gives_one_instruction_and_the_cli_says_the_error_once(broken_config, capsys, caplog):
    """Round-3 audit: the page said "start TOW again" and "reload this page: TOW reads the file
    again by itself"; a command printed the error twice (the unread language, then itself)."""
    from tow import cli, i18n

    text = _local().get("/", headers={**HTML, "Accept-Language": "en"}).text
    assert "start TOW again" not in text
    assert "reads the file again by itself" in text
    i18n._configured_language_of.cache_clear()
    assert cli.main(["status"]) == 3
    err = capsys.readouterr().err
    assert err.count("config.yaml") == 1
    assert not [entry for entry in caplog.records if entry.name == "tow.i18n"]


def test_a_config_moved_away_while_tow_runs_is_said_too(monkeypatch, capsys):
    # A move of the running install's folder that failed half-way took config.yaml along: every
    # page and /healthz answered "Internal Server Error", so tow run restarted its web server.
    import importlib

    monkeypatch.setattr(importlib.import_module("tow.web.app"), "_config_logged", [])
    path = config_path()
    good = path.read_bytes()
    path.unlink()
    try:
        page = _local().get("/", headers={**HTML, "Accept-Language": "en"})
        assert page.status_code == 503
        assert "there is no such file at" in page.text
        assert "nightly backup" in page.text
        health = _local().get("/healthz")
        assert health.status_code == 200
        assert "there is no such file" in health.json()["config_error"]
        from tow import cli

        assert cli.main(["status"]) == 3
        assert "there is no such file" in capsys.readouterr().err
    finally:
        path.write_bytes(good)


def test_a_page_names_the_file_the_place_and_the_way_back(broken_config):
    response = _local().get("/", headers={**HTML, "Accept-Language": "en"})
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    text = response.text
    assert "config.yaml" in text
    assert "line " in text  # the place of the error
    assert "nightly backup" in text  # and how to get back
    assert "Internal Server Error" not in text
    russian = _local().get("/settings", headers={**HTML, "Accept-Language": "ru"})
    assert "ночной копии" in russian.text


def test_health_json_says_it_in_json_and_healthz_still_answers(broken_config):
    from tow.supervisor.layout import install_id

    health = _local().get("/health.json")
    assert health.status_code == 503
    assert health.json()["ok"] is False
    assert any("config.yaml" in line for line in health.json()["detail"])
    alive = _local().get("/healthz").json()  # a restart could not read the file either
    assert alive["ok"] is True
    assert alive["install"] == install_id()
    assert "config.yaml" in alive["config_error"]


def test_the_network_gets_no_detail(broken_config):
    response = TestClient(app, client=("192.168.1.7", 50000)).get("/", headers=HTML)
    assert response.status_code == 503
    assert "line " not in response.text
    assert "trackers" not in response.text
    assert "config_error" not in TestClient(app, client=("192.168.1.7", 50000)).get("/healthz").json()


def test_serve_log_gets_the_problem_once(broken_config, caplog):
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        for _ in range(3):
            _local().get("/", headers=HTML)
            _local().get("/health.json", headers={"Accept-Language": "en"})
    lines = [record.getMessage() for record in caplog.records if record.name == "uvicorn.error"]
    assert len(lines) == 1
    assert lines[0].startswith("config.yaml cannot be used: config.yaml cannot be read")


def test_a_fixed_file_is_read_again_without_a_restart(broken_config):
    assert _local().get("/health.json").status_code == 503
    text = broken_config.read_text(encoding="utf-8")
    broken_config.write_text(text.replace("trackers: [unclosed\n", ""), encoding="utf-8")
    assert _local().get("/health.json").status_code == 200
