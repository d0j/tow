import re

from helpers import add_unready_client, raises_code

from tow.clients.factory import from_secrets
from tow.clients.spec import discover, ready


def test_discover_ready_clients_in_display_order(monkeypatch):
    add_unready_client(monkeypatch)
    specs = discover()
    assert specs["qbittorrent"].ready is True
    assert specs["qbittorrent"].title == "qBittorrent"
    assert [s.kind for s in ready()] == ["qbittorrent", "transmission", "deluge"]
    assert specs["draft"].ready is False
    for spec in ready():
        assert spec.steps, spec.kind
        assert {field.name for field in spec.fields} >= {"host", "port", "password"}
    assert "username" not in {field.name for field in specs["deluge"].fields}


def test_client_texts_come_from_the_language_files():
    """A client's wording lives in the language files: a wording fix touches only a language
    file, so this test checks keys, never the words."""
    import re

    from tow import i18n

    def is_key(value):
        return bool(re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", value or ""))

    for spec in ready():
        keys = [*spec.step_keys, spec.note_key, *(field.label for field in spec.field_keys)]
        keys += [field.placeholder for field in spec.field_keys if is_key(field.placeholder)]
        for key in keys:
            assert key.startswith((f"client.{spec.kind}.", "client.fields.")), key
            for code in i18n.codes():
                assert i18n.has(key, code), (code, key)
                assert i18n.translate(key, code).strip(), (code, key)
    for lang in i18n.codes():
        token = i18n._CURRENT.set(lang)  # as a request in this language
        try:
            for spec in ready():
                assert spec.steps == tuple(i18n.translate(key, lang) for key in spec.step_keys)
                assert [field.label for field in spec.fields] == [
                    i18n.translate(f.label, lang) for f in spec.field_keys
                ]
                assert spec.note == (i18n.translate(spec.note_key, lang) if spec.note_key else "")
        finally:
            i18n._CURRENT.reset(token)
    # No request: the messages' language (Russian in the tests).
    deluge = discover()["deluge"]
    assert deluge.note == i18n.translate(deluge.note_key, "ru")


def test_a_broken_client_module_does_not_take_the_others_down(monkeypatch, caplog):
    import importlib

    from tow.clients import spec

    real = importlib.import_module

    def import_module(name, *args, **kwargs):
        if name == "tow.clients.deluge":
            raise ImportError("No module named 'some_missing_dependency'")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(spec.importlib, "import_module", import_module)
    spec._discover.cache_clear()
    try:
        assert [s.kind for s in ready()] == ["qbittorrent", "transmission"]
        assert spec.SKIPPED == [("deluge", "ImportError: No module named 'some_missing_dependency'")]
        assert "deluge" in caplog.text
    finally:
        monkeypatch.undo()
        spec._discover.cache_clear()
    assert "deluge" in discover()


def test_settings_search_finds_every_plugin_by_name():
    from fastapi.testclient import TestClient

    from tow import notifiers
    from tow.web import app

    page = TestClient(app).get("/settings").text
    clients_q = re.search(r'id="acc-clients" data-q="([^"]*)"', page).group(1).split()
    notify_q = re.search(r'id="acc-bots" data-q="([^"]*)"', page).group(1).split()
    for client in ready():
        assert client.kind in clients_q
        assert client.title.casefold() in clients_q
    for kind, module in notifiers.kinds().items():
        assert kind in notify_q
        assert module.TITLE.split()[0].casefold() in notify_q
    assert "подключения" in clients_q  # the card's own words stay


def test_factory_rejects_unready_kind(monkeypatch):
    add_unready_client(monkeypatch)
    with raises_code("client.factory.not_implemented", RuntimeError):
        from_secrets({"client": {"kind": "draft"}}, {"draft": {"host": "x"}})


def test_factory_rejects_unknown_kind():
    with raises_code("client.factory.not_implemented", RuntimeError):
        from_secrets({"client": {"kind": "utorrent"}}, {})


def test_legacy_single_client_of_another_kind_uses_its_own_secrets():
    from tow.clients.factory import client_secret_block
    from tow.clients.transmission import TransmissionClient

    cfg = {"client": {"kind": "transmission"}}
    secrets = {"transmission": {"host": "nas", "port": 9091}}
    assert client_secret_block(cfg, secrets) == {"host": "nas", "port": 9091}
    adapter = from_secrets(cfg, secrets)
    assert isinstance(adapter, TransmissionClient)
    assert adapter.client_kind == "transmission"
