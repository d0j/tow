"""Client save paths accepted by TOW."""

import pytest

from tow.folders import save_path_problem


@pytest.mark.parametrize("path", [r"D:\Media\Serials", "d:/media", "/downloads/tv", "M:\\"])
def test_absolute_local_and_posix_paths_are_accepted(path):
    assert save_path_problem(path) is None


@pytest.mark.parametrize(
    "path",
    [
        "Media",
        r"..\Media",
        r"D:\Media\..\Windows",
        r"\\?\D:\Media",
        r"\\.\PhysicalDrive0",
        "D:\\Media\x00",
        r"\\attacker\share",
        "//attacker/share",
    ],
)
def test_unsafe_or_relative_paths_are_refused(path):
    assert save_path_problem(path)


def test_unc_is_allowed_only_when_enabled():
    assert save_path_problem(r"\\nas\media") is not None
    assert save_path_problem(r"\\nas\media", allow_unc=True) is None
    assert save_path_problem(r"\\nas\media\..\x", allow_unc=True) is not None


def test_add_topic_with_relative_path_is_refused_before_anything_is_saved(monkeypatch):
    from fastapi.testclient import TestClient

    from tow.store import load_state
    from tow.web import app

    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "A", "save_path": "Media"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "полный путь" in client.get(response.headers["location"]).text
    assert load_state()["topics"] == []
