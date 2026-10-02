from __future__ import annotations

from copy import deepcopy
from typing import ClassVar

import httpx

from tow.doctor import _probe_root, doctor_report
from tow.store import load_state, save_state


class _Response:
    status_code = 200
    headers: ClassVar = {}
    text = "ok"
    content = b"ok"


class _Http:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        return _Response()


def test_targeted_doctor_keeps_cached_qbit_result(monkeypatch):
    state = {
        "mirrors": {"rutor": {"active": "http://rutor"}},
        "doctor": {"qbit": "FAIL prior qbit", "ok": False, "probes": []},
    }
    saved = []
    monkeypatch.setattr("tow.doctor.load_state", lambda: deepcopy(state))
    monkeypatch.setattr("tow.doctor.save_state", saved.append)
    monkeypatch.setattr("tow.doctor.load_config", lambda: {"trackers": {"rutor": {"fetch_hosts": ["http://rutor"]}}})
    monkeypatch.setattr("tow.doctor.load_secrets", dict)
    monkeypatch.setattr("tow.doctor.http_client", lambda **kwargs: _Http())
    monkeypatch.setattr("tow.doctor._autostart", lambda: {"on": False})

    report = doctor_report(probe=True, names=["rutor"])

    assert report["qbit"] == "FAIL prior qbit"
    assert report["probes"][0]["ok"] is True
    assert saved
    assert saved[0]["doctor"]["qbit"] == "FAIL prior qbit"


def test_probe_never_follows_cross_origin_or_changes_active(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    save_state({"topics": [], "mirrors": {"demo": {"active": "https://a.example"}}})
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:9/private"})

    monkeypatch.setattr(
        "tow.doctor.load_config", lambda: {"trackers": {"demo": {"fetch_hosts": ["https://a.example"]}}}
    )
    monkeypatch.setattr("tow.doctor.load_secrets", dict)
    monkeypatch.setattr("tow.doctor._autostart", lambda: {"on": False})
    monkeypatch.setattr(
        "tow.doctor.http_client",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=kw["follow_redirects"]),
    )

    report = doctor_report(probe=True, names=["demo"])
    assert hits == ["https://a.example/"]
    assert report["probes"][0]["ok"] is False
    assert "redirect outside" in report["probes"][0]["error"]
    assert load_state()["mirrors"]["demo"]["active"] == "https://a.example"


def test_probe_caps_response_body(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tow.doctor.load_config", lambda: {"trackers": {"demo": {"fetch_hosts": ["https://a.example"]}}}
    )
    monkeypatch.setattr("tow.doctor.load_secrets", dict)
    monkeypatch.setattr("tow.doctor._autostart", lambda: {"on": False})
    monkeypatch.setattr(
        "tow.doctor.http_client",
        lambda **kw: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-length": "10000000"}, content=b"ignored", request=request
                )
            ),
            follow_redirects=kw["follow_redirects"],
        ),
    )
    report = doctor_report(probe=True, names=["demo"])
    assert report["probes"][0]["ok"] is False
    assert "exceeds" in report["probes"][0]["error"]


def test_probe_accepts_normal_large_home_page():
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 100_000, request=request))
    ) as client:
        response = _probe_root(client, "https://a.example")
    assert response.status_code == 200
    assert len(response.content) == 100_000


def _doctor_with(monkeypatch, *, state, trackers, responses):
    """Run doctor_report with fake config/state; ``responses`` maps host -> status code."""

    class _StatusHttp(_Http):
        def get(self, url):
            response = _Response()
            response.status_code = responses[url.rstrip("/")]
            return response

    saved = []
    monkeypatch.setattr("tow.doctor.load_state", lambda: deepcopy(state))
    monkeypatch.setattr("tow.doctor.save_state", saved.append)
    monkeypatch.setattr("tow.doctor.load_config", lambda: {"trackers": trackers})
    monkeypatch.setattr("tow.doctor.load_secrets", dict)
    monkeypatch.setattr("tow.doctor.http_client", lambda **kwargs: _StatusHttp())
    monkeypatch.setattr("tow.doctor._autostart", lambda: {"on": False})
    return saved


def test_partial_run_judges_the_whole_report(monkeypatch):
    # N11: re-probing a healthy tracker reported ok although qBit had failed before.
    state = {
        "doctor": {"qbit": "FAIL refused", "ok": False, "probes": [{"tracker": "a", "host": "http://a", "ok": True}]}
    }
    trackers = {"a": {"fetch_hosts": ["http://a"]}, "b": {"fetch_hosts": ["http://b"]}}
    _doctor_with(monkeypatch, state=state, trackers=trackers, responses={"http://b": 200})

    report = doctor_report(probe=True, names=["b"])

    assert report["ok"] is False


def test_one_dead_mirror_among_working_ones_is_degraded_not_failed(monkeypatch):
    trackers = {"a": {"fetch_hosts": ["http://a1", "http://a2"]}}
    _doctor_with(monkeypatch, state={}, trackers=trackers, responses={"http://a1": 503, "http://a2": 200})
    monkeypatch.setattr(
        "tow.doctor.client_factory.from_secrets", lambda *a, **k: type("C", (), {"ping": lambda s: "5.2"})()
    )

    report = doctor_report(probe=True)

    assert report["ok"] is True
    assert report["degraded"] == ["a http://a1"]


def test_removed_tracker_disappears_from_a_partial_report(monkeypatch):
    state = {"doctor": {"qbit": "5.2", "probes": [{"tracker": "gone", "host": "http://gone", "ok": False}]}}
    _doctor_with(monkeypatch, state=state, trackers={"b": {"fetch_hosts": ["http://b"]}}, responses={"http://b": 200})

    report = doctor_report(probe=True, names=["b"])

    assert [probe["tracker"] for probe in report["probes"]] == ["b"]
    assert report["ok"] is True


def test_the_report_shows_this_oses_autostart(monkeypatch):
    # 1.21: the autostart of this OS instead of the Task Scheduler's TOW-check / TOW-serve.
    from tow import doctor

    class Backend:
        def status(self):
            return {"backend": "windows", "on": True, "where": "TOW"}

    monkeypatch.setattr("tow.autostart.backend", lambda: Backend())
    assert doctor._autostart() == {"backend": "windows", "on": True, "where": "TOW"}

    def broken():
        raise OSError("no scheduler")

    monkeypatch.setattr("tow.autostart.backend", broken)
    assert doctor._autostart() == {"on": False, "error": "не удалось узнать состояние автозапуска"}
