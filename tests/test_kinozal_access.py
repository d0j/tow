"""Synthetic Kinozal route compatibility and path-specific clearance proofs."""

from __future__ import annotations

import json

import httpx
import pytest
from test_protection import CF, SPEC, TORRENT, clearance, install, settings
from test_protection_providers import ORIGIN, solution

from tow.config import load_config, save_config
from tow.mirrors import MirrorFetchError, pick_and_get
from tow.protection import chain, providers
from tow.protection.spec import public_probe_path, validate_probe_path
from tow.trackers import GenericHttpTracker, presets


@pytest.mark.parametrize("path", ["/download.php?id={id}", "/.dl./download.php?id={id}"])
def test_current_and_legacy_default_downloads_use_current_route(monkeypatch, path):
    spec = {**SPEC, "login_hosts": [], "download_path": path}
    tracker = GenericHttpTracker("kinozal", spec)
    calls = []

    def get(hosts, actual_path, *args, **kwargs):
        calls.append(actual_path)
        assert kwargs["torrent_only"] is True
        return httpx.Response(200, content=TORRENT), hosts[0]

    monkeypatch.setattr(tracker, "_get", get)
    assert tracker.fetch_torrent("https://site.example/topic/7", {}, None) == TORRENT
    assert calls == ["/download.php?id=7"]
    assert spec["download_path"] == path


def test_legacy_config_survives_read_and_save_without_rewriting_its_route():
    from tow.paths import config_path

    cfg = load_config()
    cfg["trackers"] = {"kinozal": {**SPEC, "download_path": "/.dl./download.php?id={id}"}}
    save_config(cfg)
    original = config_path().read_bytes()
    loaded = load_config()
    assert loaded["trackers"]["kinozal"]["download_path"] == "/.dl./download.php?id={id}"
    assert GenericHttpTracker("kinozal", loaded["trackers"]["kinozal"]).spec["download_path"] == "/download.php?id={id}"
    assert config_path().read_bytes() == original
    save_config(loaded)
    assert load_config()["trackers"]["kinozal"]["download_path"] == "/.dl./download.php?id={id}"


def test_custom_and_unknown_routes_remain_unchanged():
    custom = {**SPEC, "download_path": "/private-custom/{id}.torrent"}
    assert GenericHttpTracker("kinozal", custom).spec["download_path"] == custom["download_path"]
    legacy = {**SPEC, "download_path": "/.dl./download.php?id={id}"}
    assert presets.normalize_spec("other-site", legacy) is legacy
    assert public_probe_path("other-site") == "/"
    assert public_probe_path("kinozal") == "/browse.php"


@pytest.mark.parametrize(
    "path",
    [
        None,
        1,
        "//evil.example",
        "/browse.php?id=1",
        "/browse.php#secret",
        "/download.php",
        "/login",
        "/%62rowse.php",
        "/../browse.php",
    ],
)
def test_probe_rejects_arbitrary_or_credential_bearing_urls_before_network(monkeypatch, path):
    monkeypatch.setattr(providers, "_transport", lambda *a: pytest.fail("network opened"))
    with pytest.raises(ValueError, match="invalid public clearance probe"):
        validate_probe_path(path)
    with pytest.raises(ValueError, match="invalid public clearance probe"):
        providers.cookie_api({"id": "test", "url": "http://127.0.0.1:8191", "_probe_path": path}, ORIGIN, 15)


@pytest.mark.parametrize("kind", ["flaresolverr", "byparr", "flare_bypasser", "trawl"])
def test_every_service_receives_only_the_public_probe(monkeypatch, kind):
    seen = []
    real_client = httpx.Client

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=solution())

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(handle)
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    configured = {"id": "first", "url": "http://127.0.0.1:8191", "kind": kind, "_probe_path": "/browse.php"}
    assert providers.adapters()[kind](configured, ORIGIN, 15).provider == "first"
    payload = json.loads(seen[0].content)
    assert payload["url"] == ORIGIN + "/browse.php"
    assert "cookie" not in seen[0].headers
    assert "authorization" not in seen[0].headers
    assert "cookies" not in payload
    assert "_probe_path" not in payload


def test_kinozal_chain_chooses_trusted_probe_without_mutating_provider(monkeypatch):
    from test_protection_providers import clearance as provider_clearance
    from test_protection_providers import settings as provider_settings

    configured = provider_settings(max_attempts=1)
    original = dict(configured["providers"][0])
    calls = []
    monkeypatch.setattr(chain, "public_addresses", lambda *a: ["93.184.216.34"])

    def solve(provider, origin, timeout):
        calls.append(provider)
        return provider_clearance()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert len(list(chain.candidates("kinozal", ORIGIN, configured))) == 1
    assert calls[0]["_probe_path"] == "/browse.php"
    assert configured["providers"][0] == original


def test_open_root_does_not_verify_a_challenged_kinozal_catalog(monkeypatch):
    def handle(request):
        if request.url.path == "/":
            return httpx.Response(200, text="public homepage")
        return httpx.Response(403, text=CF, headers={"cf-mitigated": "challenge"})

    calls, _ = install(monkeypatch, handle)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "accept", lambda *a: pytest.fail("catalog is still challenged"))
    with pytest.raises(MirrorFetchError):
        pick_and_get("kinozal", SPEC["fetch_hosts"], "/topic/7", protection=settings())
    assert [r.url.path for r in calls if "cf_clearance=" in r.headers.get("cookie", "")] == ["/browse.php"]


@pytest.mark.parametrize("persist", [True, False])
def test_public_catalog_is_verified_before_real_resource_and_preview_never_solves(monkeypatch, persist):
    accepted = []

    def handle(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF, headers={"cf-mitigated": "challenge"})
        return (
            httpx.Response(200, text="catalog")
            if request.url.path == "/browse.php"
            else httpx.Response(200, content=TORRENT)
        )

    calls, _ = install(monkeypatch, handle)
    monkeypatch.setattr(
        chain, "candidates", lambda *a, **kw: iter([clearance()]) if persist else pytest.fail("preview solves")
    )
    monkeypatch.setattr(chain, "accept", lambda *a: accepted.append(a))
    if persist:
        response, _ = pick_and_get("kinozal", SPEC["fetch_hosts"], "/download/7", protection=settings(), persist=True)
        assert response.content == TORRENT
        assert len(accepted) == 1
        assert [r.url.path for r in calls if "cf_clearance=" in r.headers.get("cookie", "")] == [
            "/browse.php",
            "/download/7",
        ]
    else:
        with pytest.raises(MirrorFetchError):
            pick_and_get("kinozal", SPEC["fetch_hosts"], "/download/7", protection=settings(), persist=False)
        assert accepted == []


def test_redirected_probe_must_reach_an_actual_nonchallenge_page(monkeypatch):
    def handle(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        if request.url.path == "/browse.php":
            return httpx.Response(302, headers={"location": "/"})
        return httpx.Response(403, text=CF)

    calls, _ = install(monkeypatch, handle)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "accept", lambda *a: pytest.fail("redirect alone is not proof"))
    with pytest.raises(MirrorFetchError):
        pick_and_get("kinozal", SPEC["fetch_hosts"], "/topic/7", protection=settings())
    assert [r.url.path for r in calls if "cf_clearance=" in r.headers.get("cookie", "")] == [
        "/browse.php",
    ]


@pytest.mark.parametrize(
    "destination",
    ["/download.php?id=7", "/details.php?id=7", "https://evil.example/browse.php", "/browse.php?secret=x"],
)
def test_probe_redirect_never_fetches_a_private_resource_or_foreign_origin(monkeypatch, destination):
    def handle(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        assert request.url.path == "/browse.php"
        return httpx.Response(302, headers={"location": destination})

    calls, _ = install(monkeypatch, handle)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "accept", lambda *a: pytest.fail("redirect is not proof"))
    with pytest.raises(MirrorFetchError):
        pick_and_get("kinozal", SPEC["fetch_hosts"], "/topic/7", protection=settings())
    assert all(r.url.host == "site.example" for r in calls)
    assert [r.url.path for r in calls if "cf_clearance=" in r.headers.get("cookie", "")] == ["/browse.php"]


def test_verified_probe_with_a_path_specific_refusal_keeps_access_and_stops_solving(monkeypatch):
    accepted = []

    def handle(request):
        if "cf_clearance=" in request.headers.get("cookie", "") and request.url.path == "/browse.php":
            return httpx.Response(200, text="catalog")
        return httpx.Response(403, text=CF)

    install(monkeypatch, handle)

    def candidates(*args, **kwargs):
        assert kwargs["skip_cached"] is True
        yield clearance()
        pytest.fail("a second solve of this verified probe is not justified")

    monkeypatch.setattr(chain, "candidates", candidates)
    monkeypatch.setattr(chain, "discard", lambda *a, **kw: pytest.fail("access works on the probe"))
    monkeypatch.setattr(chain, "accept", lambda *a: accepted.append(a))
    with pytest.raises(MirrorFetchError) as caught:
        pick_and_get("kinozal", SPEC["fetch_hosts"], "/topic/7", protection=settings())
    assert caught.value.failure == "cloudflare"
    assert len(accepted) == 1


def test_broken_probe_endpoint_keeps_the_real_mirror_error(monkeypatch):
    def handle(request):
        if "cf_clearance=" in request.headers.get("cookie", "") and request.url.path == "/browse.php":
            return httpx.Response(404, text="missing catalog")
        return httpx.Response(403, text=CF)

    install(monkeypatch, handle)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "accept", lambda *a: pytest.fail("probe is missing"))
    with pytest.raises(MirrorFetchError) as caught:
        pick_and_get("kinozal", SPEC["fetch_hosts"], "/topic/7", protection=settings())
    assert caught.value.code == "mirrors.all_failed"
    assert caught.value.failure == "cloudflare"


@pytest.mark.parametrize(
    "values",
    [
        {"public_probe_path": "/download.php"},
        {"download_path_aliases": None},
        {"download_path_aliases": {"/old/{id}": None}},
        {"download_path_aliases": {"/old/{id}": "//evil.example/{id}"}},
    ],
)
def test_bad_optional_route_declarations_are_refused(values):
    from dataclasses import replace

    with pytest.raises(ValueError, match=r"invalid .* (probe|alias)"):
        presets._validate_routes(replace(presets.SitePreset(name="synthetic"), **values))


@pytest.mark.parametrize(
    "route",
    [
        "get.php?id=7",
        "get.php?t=7",
        "download.php?t=7",
        ".dl./get.php?id=7",
        "download.php?foo=1&id=7",
        "details.php?foo=1&id=7&bar=2",
        "download.php?id=7&t=2",
        "download.php?id=7&t=abc",
    ],
)
def test_every_recognized_download_link_has_a_usable_id(route):
    from tow.guess import guess_from_url

    url = "https://kinozal.guru/" + route
    spec = guess_from_url(url)
    assert GenericHttpTracker("kinozal", spec).parse_id(url) == "7"
    assert GenericHttpTracker("kinozal", spec).parse_id("https://kinozal.guru/details.php?id=7") == "7"


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("synthetic"), httpx.ConnectError("synthetic")])
def test_optional_probe_exception_preserves_the_real_resource_error(monkeypatch, failure):
    def handle(request):
        if request.url.path == "/browse.php" and "cf_clearance=" in request.headers.get("cookie", ""):
            raise failure
        return httpx.Response(403, text=CF)

    install(monkeypatch, handle)

    def candidates(*a, **kw):
        yield clearance()
        pytest.fail("an optional outage cannot justify another solve")

    monkeypatch.setattr(chain, "candidates", candidates)
    monkeypatch.setattr(chain, "discard", lambda *a, **kw: pytest.fail("outage cannot revoke access"))
    with pytest.raises(MirrorFetchError) as caught:
        pick_and_get("kinozal", SPEC["fetch_hosts"], "/topic/7", protection=settings())
    assert caught.value.code == "mirrors.all_failed"
    assert caught.value.failure == "cloudflare"
