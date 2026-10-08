from __future__ import annotations

from copy import deepcopy
from typing import ClassVar

import httpx
import pytest

from tow.doctor import _probe_root, doctor_report
from tow.store import load_state, save_state


@pytest.mark.parametrize("client_result", ["5.2", "FAIL prior client"])
@pytest.mark.parametrize("configured", [True, False])
def test_cached_network_report_refreshes_local_inventory_without_probing_or_saving(
    monkeypatch, client_result, configured
):
    from tow import doctor

    cached = {
        "python": "old-python",
        "topics": 99,
        "trackers": ["old-site"],
        "qbit_host_set": not configured,
        "notify_set": not configured,
        "autostart": {"on": not configured},
        "qbit": client_result,
        "probes": [{"tracker": "demo", "host": "https://demo.example", "ok": False, "error": "http 503"}],
        "ok": False,
        "degraded": ["demo https://demo.example"],
    }
    state = {"topics": [{"id": "a"}, {"id": "b"}], "doctor": cached}
    before = deepcopy(state)
    monkeypatch.setattr(doctor, "load_state", lambda: state)
    monkeypatch.setattr(doctor, "load_config", lambda: {"trackers": {"demo": {}}})
    monkeypatch.setattr(
        doctor,
        "load_secrets",
        lambda: (
            {"qbittorrent": {"host": "http://client.example"}, "telegram": {"token": "test", "chat_ids": ["123"]}}
            if configured
            else {}
        ),
    )
    monkeypatch.setattr(doctor, "_py", lambda: "current-python")
    monkeypatch.setattr(doctor, "_autostart", lambda: {"on": configured, "where": "test-service"})

    def forbidden(*args, **kwargs):
        pytest.fail("A cached report must not probe the network or write state")

    monkeypatch.setattr(doctor, "http_client", forbidden)
    monkeypatch.setattr(doctor.client_factory, "from_secrets", forbidden)
    monkeypatch.setattr(doctor, "save_state", forbidden)

    report = doctor_report(probe=False)

    assert report["python"] == "current-python"
    assert report["topics"] == 2
    assert report["trackers"] == ["demo"]
    assert report["qbit_host_set"] is configured
    assert report["notify_set"] is configured
    assert report["autostart"] == {"on": configured, "where": "test-service"}
    for key in ("qbit", "probes", "ok", "degraded"):
        assert report[key] == cached[key]
    assert state == before
    report["probes"][0]["ok"] = True
    report["degraded"].clear()
    assert state == before


def test_doctor_page_does_not_render_the_cached_python_and_topic_count(monkeypatch):
    from fastapi.testclient import TestClient

    from tow import doctor
    from tow.web import app

    state = {
        "topics": [{"id": "a"}, {"id": "b"}],
        "doctor": {"python": "old-python", "topics": 99, "qbit": "FAIL prior client", "probes": [], "ok": False},
    }
    monkeypatch.setattr(doctor, "load_state", lambda: state)
    monkeypatch.setattr(doctor, "load_config", lambda: {"trackers": {}})
    monkeypatch.setattr(doctor, "load_secrets", dict)
    monkeypatch.setattr(doctor, "_py", lambda: "current-python")
    monkeypatch.setattr(doctor, "_autostart", lambda: {"on": False})

    response = TestClient(app).get("/doctor")

    assert response.status_code == 200
    assert "current-python" in response.text
    assert "old-python" not in response.text
    assert '<div class="v">2</div>' in response.text
    assert '<div class="v">99</div>' not in response.text
    assert "prior client" in response.text


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


def test_a_mirror_behind_cloudflares_check_is_reported_as_cloudflare(monkeypatch):
    """Cloudflare's own header was not looked at: a check page of another title was "http 403"."""

    class _ChallengeHttp(_Http):
        def get(self, url):
            response = _Response()
            response.status_code = 403
            response.headers = {"cf-mitigated": "challenge"}
            response.text = "<html><title>Один момент…</title></html>"
            return response

    monkeypatch.setattr("tow.doctor.load_state", dict)
    monkeypatch.setattr("tow.doctor.save_state", lambda state: None)
    monkeypatch.setattr("tow.doctor.load_config", lambda: {"trackers": {"a": {"fetch_hosts": ["http://a"]}}})
    monkeypatch.setattr("tow.doctor.load_secrets", dict)
    monkeypatch.setattr("tow.doctor.http_client", lambda **kwargs: _ChallengeHttp())
    monkeypatch.setattr("tow.doctor._autostart", lambda: {"on": False})

    report = doctor_report(probe=True, names=["a"])

    assert [probe.get("error") for probe in report["probes"]] == ["cloudflare"]


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
    assert doctor._autostart() == {"backend": "windows", "on": True, "where": "TOW", "foreign": False}

    def broken():
        raise OSError("no scheduler")

    monkeypatch.setattr("tow.autostart.backend", broken)
    assert doctor._autostart() == {"on": False, "error": "не удалось узнать состояние автозапуска"}


@pytest.mark.parametrize(
    ("status", "foreign"),
    [
        ({"on": False, "ours": False, "stale": False, "state": "present", "where": "TOW"}, True),  # Windows
        ({"on": False, "ours": False, "stale": False, "where": "tow.service"}, True),  # systemd, launchd
        ({"on": False, "ours": False, "stale": True, "state": "present", "where": "TOW"}, False),  # its program is gone
        ({"on": False, "ours": False, "stale": False, "state": "absent", "where": "TOW"}, False),
        ({"on": True, "ours": True, "stale": False, "state": "present", "where": "TOW"}, False),
    ],
)
def test_another_folders_autostart_is_never_shown_as_this_ones(monkeypatch, status, foreign):
    """Round-3 audit: Diagnostics showed the task of another TOW folder next to this one's name."""
    from fastapi.testclient import TestClient

    from tow.i18n import t
    from tow.web import app

    class Backend:
        def status(self):
            return dict(status)

    monkeypatch.setattr("tow.autostart.backend", lambda: Backend())
    page = TestClient(app).get("/doctor", headers={"Accept-Language": "ru"}).text
    assert (t("doctor.autostart_foreign", "ru", where=status["where"]) in page) is foreign


def test_tow_doctor_says_its_findings_in_the_owners_words(monkeypatch):
    """Round-3 audit: `tow doctor` in Russian printed "cloudflare", "redirect outside configured
    mirror" and the client library's English as they came."""
    from tow import doctor
    from tow.i18n import t

    monkeypatch.setattr(doctor, "owner_language", lambda: "ru")
    report = {
        "ok": False,
        "qbit": "FAIL [WinError 10061] No connection could be made",
        "probes": [
            {"tracker": "a", "host": "https://a.example", "ok": False, "error": "cloudflare"},
            {"tracker": "a", "host": "https://b.example", "ok": False, "error": "redirect outside configured mirror"},
            {"tracker": "a", "host": "https://c.example", "ok": False, "error": "http 503"},
        ],
    }
    text = doctor.doctor_text(report)
    for key in ("doctor.reason.cloudflare", "doctor.reason.redirect", "doctor.reason.refused"):
        assert t(key, "ru") in text
    assert t("doctor.reason.http", "ru", code="503") in text
    assert "outside configured mirror" not in text
    assert "No connection" not in text
