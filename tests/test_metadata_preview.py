"""Native previews with fake clients: no live peers or transfer-task operations."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from helpers import make_torrent

from tow import content, net_guard, trackers
from tow.clients import factory, qbittorrent
from tow.clients.managed import ClientError
from tow.errors import TowError
from tow.torrent import parse_torrent_metadata
from tow.web import app, services

URL = "https://tracker.example/topic/1"
TORRENT = make_torrent({b"name": b"file.mkv", b"length": 10, b"piece length": 16384, b"pieces": b"x" * 20})
META = parse_torrent_metadata(TORRENT)
MAGNET = f"magnet:?xt=urn:btih:{META.hash_v1}&tr=https%3A%2F%2Ftracker.example%2Fann%3Fk%3Dx%26uid%3Dy"


class PreviewAPI:
    def __init__(self, version="2.11.9", existing=False):
        self.app = SimpleNamespace(web_api_version=version)
        self.existing = existing
        self.calls = []
        self.responses = [{"infohash_v1": META.hash_v1}, {"info": {"files": [{}]}}]
        self.content = TORRENT
        self.error = None

    def torrents_info(self, **kwargs):
        self.calls.append("info")
        return [SimpleNamespace(hash=META.client_hash)] if self.existing else []

    def torrents_export(self, **kwargs):
        self.calls.append("export")
        return self.content

    def torrents_fetch_metadata(self, *, source):
        assert unquote(source) == MAGNET, "retain escaped tracker query delimiters"
        self.calls.append("fetch")
        if self.error:
            raise self.error
        return self.responses.pop(0)

    def torrents_save_metadata(self, *, source):
        assert unquote(source) == MAGNET
        self.calls.append("save")
        return self.content

    def __getattr__(self, name):
        pytest.fail(f"preview invoked an unexpected client operation: {name}")


def client(monkeypatch, api):
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    monkeypatch.setattr(qbittorrent.time, "sleep", lambda _seconds: None)
    return qbittorrent.QBittorrentClient("http://qbit.example", 8080, "user", "password")


def test_native_preview_polls_hash_only_response_and_exports_verified_metadata(monkeypatch):
    api = PreviewAPI()
    assert client(monkeypatch, api).preview_magnet(MAGNET) == TORRENT
    assert [op for op in api.calls if op != "info"] == ["fetch", "fetch", "save"]


@pytest.mark.parametrize("version", ["2.8.14", "2.11.8", "unknown", ""])
def test_old_api_refuses_new_magnet_without_any_transfer_mutation(monkeypatch, version):
    api = PreviewAPI(version)
    with pytest.raises(ClientError) as error:
        client(monkeypatch, api).preview_magnet(MAGNET)
    assert error.value.code == "content.magnet_unsupported"
    assert set(api.calls) == {"info"}


@pytest.mark.parametrize("read_only", [False, True])
def test_existing_foreign_torrent_is_only_exported_on_old_api(monkeypatch, read_only):
    api = PreviewAPI("2.8.14", existing=True)
    adapter = client(monkeypatch, api)
    adapter.read_only = read_only
    assert adapter.preview_magnet(MAGNET) == TORRENT
    assert api.calls == ["info", "export"]


def test_dry_run_never_starts_native_peer_request(monkeypatch):
    api = PreviewAPI()
    adapter = client(monkeypatch, api)
    adapter.read_only = True
    with pytest.raises(ClientError) as error:
        adapter.preview_magnet(MAGNET)
    assert error.value.code == "content.magnet_unsupported"
    assert set(api.calls) == {"info"}


def test_native_timeout_does_not_delete_or_stop_any_hash(monkeypatch):
    api = PreviewAPI()
    adapter = client(monkeypatch, api)
    clock = iter([0, 0, 46])
    monkeypatch.setattr(qbittorrent.time, "monotonic", lambda: next(clock))
    with pytest.raises(ClientError) as error:
        adapter.preview_magnet(MAGNET)
    assert error.value.code == "content.magnet_timeout"
    assert [op for op in api.calls if op != "info"] == ["fetch"]


@pytest.mark.parametrize("existing", [False, True])
def test_preview_rejects_other_torrent_data(monkeypatch, existing):
    api = PreviewAPI(existing=existing)
    api.responses = [{"info": {"files": [{}]}}]
    api.content = make_torrent({b"name": b"other.mkv", b"length": 0, b"piece length": 16384, b"pieces": b""})
    with pytest.raises(ClientError) as error:
        client(monkeypatch, api).preview_magnet(MAGNET)
    assert error.value.code == "client.qbittorrent.magnet_data_mismatch"


def test_api_fault_is_safe_and_never_falls_back_to_add(monkeypatch):
    api = PreviewAPI()
    api.error = RuntimeError("private client response")
    with pytest.raises(ClientError) as error:
        client(monkeypatch, api).preview_magnet(MAGNET)
    assert error.value.code == "content.magnet_failed"
    assert "private" not in str(error.value)


def test_torrent_added_by_another_user_during_prefetch_is_only_exported(monkeypatch):
    api = PreviewAPI()

    def fetch(*, source):
        api.existing = True
        api.calls.append("fetch")
        return {"info": {"files": [{}]}}

    api.torrents_fetch_metadata = fetch
    assert client(monkeypatch, api).preview_magnet(MAGNET) == TORRENT
    assert [op for op in api.calls if op != "info"] == ["fetch", "export"]


def test_invalid_magnet_never_reaches_client(monkeypatch):
    api = PreviewAPI()
    with pytest.raises(ClientError) as error:
        client(monkeypatch, api).preview_magnet("https://tracker.example/")
    assert error.value.code == "client.qbittorrent.magnet_invalid"
    assert api.calls == []


@pytest.mark.parametrize("kind", ["v2", "hybrid"])
def test_native_preview_validates_full_v2_and_hybrid_hashes(monkeypatch, kind):
    info = {
        b"name": b"Show",
        b"piece length": 16384,
        b"meta version": 2,
        b"file tree": {b"e01.mkv": {b"": {b"length": 10, b"pieces root": b"r" * 32}}},
    }
    if kind == "hybrid":
        info.update({b"files": [{b"length": 10, b"path": [b"e01.mkv"]}], b"pieces": b"x" * 20})
    data = make_torrent(info)
    meta = parse_torrent_metadata(data)
    magnet = f"magnet:?xt=urn:btmh:1220{meta.hash_v2}"
    if kind == "hybrid":
        magnet += f"&xt=urn:btih:{meta.hash_v1}"
    api = PreviewAPI()
    api.content = data
    api.torrents_fetch_metadata = lambda **kwargs: {"info": {"files": [{}]}}
    api.torrents_save_metadata = lambda **kwargs: data
    adapter = client(monkeypatch, api)
    assert adapter.preview_magnet(magnet) == data
    with pytest.raises(ClientError) as error:
        adapter.preview_magnet(f"magnet:?xt=urn:btmh:1220{'a' * 64}")
    assert error.value.code == "client.qbittorrent.magnet_data_mismatch"


def service_fixture(monkeypatch, *, capable=True, enabled=True, data=TORRENT):
    calls = []
    adapter = SimpleNamespace(
        capabilities={"metadata_preview": capable},
        preview_magnet=lambda magnet: calls.append(("preview", magnet)) or data,
        materialize_magnet=lambda *_args: pytest.fail("mutating fallback must never run"),
    )
    tracker = SimpleNamespace(
        fetch_magnet=lambda *args, **kwargs: calls.append(("tracker", kwargs)) or (MAGNET, META.hash_v1),
        fetch_torrent=lambda *_args, **_kwargs: pytest.fail("must not consume torrent quota"),
    )
    monkeypatch.setattr(services, "load_config", lambda: {"clients": [{"id": "main", "enabled": enabled}]})
    monkeypatch.setattr(trackers, "load_trackers", lambda *_args: {})
    monkeypatch.setattr(trackers, "match_tracker", lambda *_args: tracker)
    monkeypatch.setattr(factory, "from_secrets", lambda *_args: adapter)
    monkeypatch.setattr(net_guard, "public_addresses", _fake_dns)
    return calls, adapter, tracker


def _fake_dns(host, _port):
    """tracker.example is public, lan.example a home-network name; no real lookup."""
    import httpcore

    answers = {"tracker.example": ["203.0.113.5"], "lan.example": ["192.168.1.20"]}
    if host not in answers or answers[host][0].startswith("192.168."):
        raise httpcore.ConnectError("not public")
    return answers[host]


def test_preview_passes_only_the_hashes_and_public_trackers_to_the_client(monkeypatch):
    calls, _, tracker = service_fixture(monkeypatch)
    page_magnet = (
        f"magnet:?xt=urn:btih:{META.hash_v1}&dn=Show+A&x.pe=192.168.1.1:80"
        "&tr=http%3A%2F%2F192.168.1.1%2Fcgi-bin%2Freboot&tr=http%3A%2F%2Flan.example%2Fx"
        "&tr=udp%3A%2F%2Ftracker.example%3A6969%2Fannounce&tr=file%3A%2F%2F%2Fetc&ws=http%3A%2F%2F10.0.0.1%2F"
        "&tr=http%3A%2F%2Funknown.example%2Fa"
    )
    tracker.fetch_magnet = lambda *_args, **_kwargs: (page_magnet, META.hash_v1)

    services.prepare_magnet_content(URL, "main")

    [preview] = [magnet for kind, magnet in calls if kind == "preview"]
    assert preview == f"magnet:?xt=urn:btih:{META.hash_v1}&tr=udp%3A%2F%2Ftracker.example%3A6969%2Fannounce"


def test_a_lan_tracker_is_kept_when_the_owner_allows_private_tracker_hosts(monkeypatch):
    calls, _, tracker = service_fixture(monkeypatch)
    monkeypatch.setattr(
        services,
        "load_config",
        lambda: {"clients": [{"id": "main", "enabled": True}], "allow_private_tracker_hosts": True},
    )
    lan = f"magnet:?xt=urn:btih:{META.hash_v1}&tr=http%3A%2F%2F192.168.1.1%2Fannounce&x.pe=192.168.1.1:80"
    tracker.fetch_magnet = lambda *_args, **_kwargs: (lan, META.hash_v1)

    services.prepare_magnet_content(URL, "main")

    [preview] = [magnet for kind, magnet in calls if kind == "preview"]
    assert preview == f"magnet:?xt=urn:btih:{META.hash_v1}&tr=http%3A%2F%2F192.168.1.1%2Fannounce"


def test_explicit_native_service_stores_bound_metadata_without_quota_download(monkeypatch):
    calls, _, _ = service_fixture(monkeypatch)
    prepared = services.prepare_magnet_content(URL, "main")
    assert content.read(prepared["token"], URL, "main") == TORRENT
    assert calls == [("tracker", {"persist": True}), ("preview", MAGNET)]
    assert services._MAGNET_PREVIEWS._value == 2


@pytest.mark.parametrize(
    ("capable", "enabled", "code"),
    [(False, True, "content.magnet_unsupported"), (True, False, "web.topics.client_disabled")],
)
def test_unsupported_and_disabled_clients_do_not_even_fetch_magnet_page(monkeypatch, capable, enabled, code):
    calls, _, _ = service_fixture(monkeypatch, capable=capable, enabled=enabled)
    with pytest.raises(TowError) as error:
        services.prepare_magnet_content(URL, "main")
    assert error.value.code == code
    assert calls == []


def test_service_rechecks_metadata_identity_before_caching(monkeypatch):
    wrong = make_torrent({b"name": b"wrong", b"length": 0, b"piece length": 16384, b"pieces": b""})
    service_fixture(monkeypatch, data=wrong)
    monkeypatch.setattr(content, "prepare", lambda *_args: pytest.fail("unverified metadata cached"))
    with pytest.raises(TowError) as error:
        services.prepare_magnet_content(URL, "main")
    assert error.value.code == "content.magnet_failed"
    assert services._MAGNET_PREVIEWS._value == 2


def test_backend_bounds_native_workers_even_after_browser_closes(monkeypatch):
    calls, _, _ = service_fixture(monkeypatch)
    lock = services._MAGNET_PREVIEWS
    assert lock.acquire(blocking=False)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(TowError) as error:
            services.prepare_magnet_content(URL, "main")
        assert error.value.code == "content.magnet_busy"
        assert calls == []
    finally:
        lock.release()
        lock.release()


def test_invalid_tracker_identity_does_not_reach_native_client(monkeypatch):
    calls, _, tracker = service_fixture(monkeypatch)
    tracker.fetch_magnet = lambda *_args, **_kwargs: (MAGNET, "a" * 40)
    with pytest.raises(TowError) as error:
        services.prepare_magnet_content(URL, "main")
    assert error.value.code == "content.magnet_failed"
    assert calls == []


def test_a_magnet_with_half_a_character_is_refused_before_the_client(monkeypatch):
    # "&xt=\udfff" was ignored as an unknown xt and then broke quote() with a UnicodeEncodeError.
    from tow.torrent import parse_magnet_hashes, preview_magnet

    broken = f"magnet:?xt=urn:btih:{META.hash_v1}&xt=\udfff"
    assert parse_magnet_hashes(broken) is None
    assert preview_magnet(broken, lambda _tracker: True) is None
    named = f"magnet:?xt=urn:btih:{META.hash_v1}&dn=Сериал А"  # other non-ASCII text stays fine
    assert preview_magnet(named, lambda _tracker: True) == f"magnet:?xt=urn:btih:{META.hash_v1}"
    calls, _, tracker = service_fixture(monkeypatch)
    tracker.fetch_magnet = lambda *_args, **_kwargs: (broken, META.hash_v1)
    with pytest.raises(TowError) as error:
        services.prepare_magnet_content(URL, "main")
    assert error.value.code == "content.magnet_failed"
    assert calls == []


def test_web_native_preparation_is_only_an_explicit_post(monkeypatch):
    calls = []
    monkeypatch.setattr(
        services, "prepare_magnet_content", lambda *args: calls.append(args) or {"token": "t", "files": []}
    )
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    assert client.get("/").status_code == 200
    assert calls == []
    response = client.post("/content/prepare", data={"url": URL, "client_id": "main", "source": "magnet"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls == [(URL, "main")]


@pytest.mark.parametrize(("source", "upload"), [("other", False), ("magnet", True)])
def test_web_rejects_ambiguous_preparation_sources(monkeypatch, source, upload):
    monkeypatch.setattr(services, "prepare_magnet_content", lambda *_args: pytest.fail("ambiguous native request"))
    monkeypatch.setattr(services, "prepare_content", lambda *_args: pytest.fail("ambiguous torrent request"))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        "/content/prepare",
        data={"url": URL, "source": source},
        files={"torrent": ("show.torrent", TORRENT)} if upload else None,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "content.changed"
