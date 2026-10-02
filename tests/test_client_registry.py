from types import SimpleNamespace

from helpers import raises_code

from tow.clients.factory import client_configurations, default_client_id


def test_client_registry_supports_multiple_clients():
    cfg = {
        "client": {"kind": "qbittorrent"},
        "clients": [
            {"id": "qbit-main", "kind": "qbittorrent", "enabled": True, "default": True},
            {"id": "deluge-main", "kind": "deluge", "enabled": False},
            {"id": "transmission-main", "kind": "transmission", "enabled": True},
        ],
    }
    rows = client_configurations(cfg)
    assert [x["id"] for x in rows] == ["qbit-main", "deluge-main", "transmission-main"]
    assert default_client_id(cfg) == "qbit-main"


def test_legacy_client_is_default():
    from tow.clients.factory import client_configuration

    cfg = {"client": {"kind": "qbittorrent"}}
    rows = client_configurations(cfg)
    assert rows[0]["id"] == "default"
    assert default_client_id(cfg) == "default"
    assert client_configuration(cfg)["kind"] == "qbittorrent"


def test_client_ids_must_be_unique_and_non_empty():

    with raises_code("client.factory.empty_id", ValueError):
        client_configurations({"clients": [{"id": "", "kind": "qbittorrent"}]})
    with raises_code("client.factory.duplicate_id", ValueError):
        client_configurations(
            {"clients": [{"id": "main", "kind": "qbittorrent"}, {"id": "main", "kind": "qbittorrent"}]}
        )


def test_from_secrets_routes_selected_instance(monkeypatch):
    from tow.clients import factory

    seen = []

    class Spec:
        kind = "qbittorrent"
        ready = True
        secrets_key = "qbittorrent"

        def load(self, secrets):
            seen.append(secrets)
            return SimpleNamespace()

    monkeypatch.setattr(factory, "get", lambda kind: Spec())
    cfg = {"clients": [{"id": "main", "kind": "qbittorrent"}, {"id": "backup", "kind": "qbittorrent"}]}
    secrets = {"clients": {"main": {"host": "main-host"}, "backup": {"host": "backup-host"}}}

    factory.from_secrets(cfg, secrets, "backup")

    assert seen == [{"qbittorrent": {"host": "backup-host"}}]
