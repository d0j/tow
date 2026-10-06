from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tow.config import load_config, save_config
from tow.store import load_secrets, load_state, save_secrets, save_state
from tow.web import app


@pytest.fixture(autouse=True)
def _public_test_hosts(monkeypatch):
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])


class _Stores:
    """The real config, state and secrets of the test's TOW_HOME, read fresh on every access."""

    def __getitem__(self, name):
        return {"cfg": load_config, "state": load_state, "secrets": load_secrets}[name]()


def _setup(monkeypatch):
    cfg = load_config()
    cfg["trackers"] = {"rutor": {"title": "rutor", "url_regex": r"rutor/(\d+)"}}
    save_config(cfg)
    save_state({"topics": [], "mirrors": {"rutor": {"active": "http://rutor"}}})
    save_secrets({"trackers": {"rutor": {"username": "fixture-user", "password": "fixture-secret"}}})
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)
    stores = _Stores()
    return _View(stores, "cfg"), _View(stores, "state"), _View(stores, "secrets")


class _View:
    """One store as a mapping that always shows what is on disk now."""

    def __init__(self, stores, name):
        self._stores, self._name = stores, name

    def _now(self):
        return self._stores[self._name]

    def __getitem__(self, key):
        return self._now()[key]

    def __contains__(self, key):
        return key in self._now()

    def __setitem__(self, key, value):
        data = self._now()
        data[key] = value
        {"cfg": save_config, "state": save_state, "secrets": save_secrets}[self._name](data)


def test_site_delete_and_undo_reconcile_config_mirrors_and_tracker_secrets(monkeypatch):
    cfg, state, secrets = _setup(monkeypatch)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    deleted = client.post("/sites/rutor/delete", follow_redirects=False)

    assert deleted.status_code == 303
    assert "rutor" not in cfg["trackers"]
    assert "rutor" not in state["mirrors"]
    assert "rutor" not in secrets["trackers"]
    assert "password" not in state["undo"]

    restored = client.post("/undo", follow_redirects=False)

    assert restored.status_code == 303
    assert cfg["trackers"]["rutor"]["title"] == "rutor"
    assert state["mirrors"]["rutor"]["active"] == "http://rutor"
    assert secrets["trackers"]["rutor"]["username"] == "fixture-user"
    assert secrets["trackers"]["rutor"]["password"] == "fixture-secret"


def test_readding_deleted_site_invalidates_older_undo(monkeypatch):
    cfg, state, secrets = _setup(monkeypatch)
    monkeypatch.setattr("tow.web.services.doctor_report", lambda **_kwargs: {})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    assert client.post("/sites/rutor/delete", follow_redirects=False).status_code == 303
    assert state["undo"]["kind"] == "site"
    response = client.post(
        "/sites/new",
        data={
            "name": "rutor",
            "url_regex": r"rutor/(\d+)",
            "fetch_hosts": "https://rutor.example",
            "download_path": "/download/{id}",
            "username": "new-user",
            "password": "new-password",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "undo" not in state
    assert secrets["trackers"]["rutor"]["password"] == "new-password"
    assert cfg["trackers"]["rutor"]["fetch_hosts"] == ["https://rutor.example"]


def test_saving_site_login_invalidates_older_site_undo(monkeypatch):
    _cfg, state, secrets = _setup(monkeypatch)
    state["undo"] = {"kind": "site", "name": "rutor", "ts": "2026-09-24T12:00:00+03:00"}
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post(
        "/sites/rutor/login", data={"username": "latest", "password": "new-password"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "undo" not in state
    assert secrets["trackers"]["rutor"]["password"] == "new-password"


def test_site_rename_and_undo_reconcile_all_stores(monkeypatch):
    cfg, state, secrets = _setup(monkeypatch)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    renamed = client.post(
        "/sites/rutor",
        data={
            "fetch_hosts": "http://rutor",
            "url_regex": r"rutor/(\d+)",
            "download_path": "/download/{id}",
            "login_path": "",
            "new_name": "rutor_new",
        },
        follow_redirects=False,
    )

    assert renamed.status_code == 303
    assert "rutor" not in cfg["trackers"]
    assert cfg["trackers"]["rutor_new"]["title"] == "rutor_new"
    assert "rutor" not in state["mirrors"]
    assert state["mirrors"]["rutor_new"]["active"] == "http://rutor"
    assert "rutor" not in secrets["trackers"]
    assert secrets["trackers"]["rutor_new"]["password"] == "fixture-secret"
    assert "password" not in state["undo"]

    restored = client.post("/undo", follow_redirects=False)

    assert restored.status_code == 303
    assert "rutor_new" not in cfg["trackers"]
    assert cfg["trackers"]["rutor"]["title"] == "rutor"
    assert "rutor_new" not in state["mirrors"]
    assert state["mirrors"]["rutor"]["active"] == "http://rutor"
    assert "rutor_new" not in secrets["trackers"]
    assert secrets["trackers"]["rutor"]["password"] == "fixture-secret"


def test_edit_does_not_expand_login_hosts_to_new_fetch_mirror(monkeypatch):
    cfg, _state, _secrets = _setup(monkeypatch)
    cfg["trackers"] = {
        "rutor": {
            "title": "rutor",
            "url_regex": r"rutor/(\d+)",
            "fetch_hosts": ["https://login.example"],
            "login_hosts": ["https://login.example"],
        }
    }
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post(
        "/sites/rutor",
        data={
            "fetch_hosts": "https://login.example\nhttps://new-mirror.example",
            "url_regex": r"rutor/(\d+)",
            "download_path": "/download/{id}",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert cfg["trackers"]["rutor"]["fetch_hosts"] == ["https://login.example", "https://new-mirror.example"]
    assert cfg["trackers"]["rutor"]["login_hosts"] == ["https://login.example"]

    response = client.post(
        "/sites/rutor",
        data={
            "fetch_hosts": "https://new-mirror.example",
            "url_regex": r"rutor/(\d+)",
            "download_path": "/download/{id}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert cfg["trackers"]["rutor"]["login_hosts"] == []

    # Adding a login host without re-entering the password is refused: the stored
    # password must not be posted to a host it was not entered for.
    refused = client.post(
        "/sites/rutor",
        data={
            "fetch_hosts": "https://new-mirror.example",
            "login_hosts": "https://new-mirror.example",
            "url_regex": r"rutor/(\d+)",
            "download_path": "/download/{id}",
        },
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert "flash=" in refused.headers["location"]
    assert cfg["trackers"]["rutor"]["login_hosts"] == []

    response = client.post(
        "/sites/rutor",
        data={
            "fetch_hosts": "https://new-mirror.example",
            "login_hosts": "https://new-mirror.example",
            "url_regex": r"rutor/(\d+)",
            "download_path": "/download/{id}",
            "username": "fixture-user",
            "password": "fixture-secret",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert cfg["trackers"]["rutor"]["login_hosts"] == ["https://new-mirror.example"]

    response = client.post(
        "/sites/rutor",
        data={
            "fetch_hosts": "https://new-mirror.example",
            "login_hosts_present": "1",
            "login_hosts": "",
            "url_regex": r"rutor/(\d+)",
            "download_path": "/download/{id}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert cfg["trackers"]["rutor"]["login_hosts"] == []


def test_freeze_unknown_site_does_not_create_mirror_bucket(monkeypatch):
    _cfg, state, _secrets = _setup(monkeypatch)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post("/sites/unknown/freeze", follow_redirects=False)

    assert response.status_code == 303
    assert "unknown" not in state["mirrors"]


def test_site_transaction_journal_recovers_all_store_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"bind: 127.0.0.1\n")
    monkeypatch.setenv("TOW_CONFIG", str(config_path))
    from tow.store import encrypted_secrets_path, secret_undo_path, state_path
    from tow.store_transaction import begin, recover

    state_path().write_bytes(b'{"topics": []}\n')
    encrypted_secrets_path().write_bytes(b"encrypted-before")
    secret_undo_path().write_bytes(b"undo-before")
    before = {
        "config": config_path.read_bytes(),
        "state": state_path().read_bytes(),
        "secrets": encrypted_secrets_path().read_bytes(),
        "undo": secret_undo_path().read_bytes(),
    }

    begin()
    config_path.write_bytes(b"bind: 0.0.0.0\n")
    state_path().write_bytes(b'{"topics": ["mixed"]}\n')
    encrypted_secrets_path().write_bytes(b"encrypted-mixed")
    secret_undo_path().write_bytes(b"undo-mixed")

    recover()

    assert config_path.read_bytes() == before["config"]
    assert state_path().read_bytes() == before["state"]
    assert encrypted_secrets_path().read_bytes() == before["secrets"]
    assert secret_undo_path().read_bytes() == before["undo"]


def test_any_process_recovers_an_interrupted_site_transaction_before_it_writes():
    """M4: recovery ran only from web requests; a scheduled check after a crash read and
    rewrote half-committed stores. It is now a persistence recovery hook."""
    from tow import site_journal
    from tow.check import record_check_failure
    from tow.paths import config_path
    from tow.store import load_state, persistence_lock, state_path
    from tow.store_transaction import begin

    config_before = config_path().read_bytes()
    state_path().write_text('{"topics": [{"id": "kept"}], "mirrors": {}}\n', encoding="utf-8")
    with persistence_lock():  # the web process, mid-transaction, then gone
        begin()
        config_path().write_bytes(b"trackers: {}\n")
        state_path().write_text('{"topics": [], "mirrors": {}}\n', encoding="utf-8")
    assert site_journal.journal_root().exists()

    record_check_failure(RuntimeError("x"), how="auto")  # a check process: no web request at all

    assert not site_journal.journal_root().exists()
    assert config_path().read_bytes() == config_before
    state = load_state()
    assert [topic["id"] for topic in state["topics"]] == ["kept"]  # restored first...
    assert state["health"]["check_ok"] is False  # ...then the check's own write landed


def test_the_recovery_hook_is_registered_by_the_store_itself():
    import tow.store
    from tow import site_journal, store_transaction

    assert ("tow.site_journal", "recover_site_journal", None) in tow.store.RECOVERY_STEPS
    # The web writes the journal that the hook reads: one format, one place, the same stores.
    assert store_transaction.journal_root() == site_journal.journal_root()
    assert store_transaction.journal_targets() == site_journal.journal_targets()


def test_a_journal_with_a_bad_exists_flag_is_a_controlled_503_not_a_crash():
    # Found while raising coverage: this one check raised TypeError, which the
    # middleware does not catch, so every request crashed with a 500.
    import json

    from fastapi.testclient import TestClient

    from tow.store_transaction import begin
    from tow.web import app

    root = begin()
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["targets"][0]["exists"] = "yes"
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    response = TestClient(app, raise_server_exceptions=False).get("/healthz")

    assert response.status_code == 503
