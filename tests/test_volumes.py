"""A folder off the disk TOW recorded for it: said in Needs attention, and accepted as the same disk
only on this computer, only with what TOW keeps there found on it now (tow.volumes)."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from helpers import flash_kind, flash_of, open_network

from tow import platform
from tow.i18n import t
from tow.issues import Snapshot, collect
from tow.progress import reconcile_topic
from tow.store import load_download_history, load_state, save_download_history, save_state

ORIGIN = {"Origin": "http://127.0.0.1"}


class FakeClient:
    client_id = "fake-main"
    client_kind = "fake"
    capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True, "completion_time": False}

    def inspect_torrent(self, infohash):
        return {"hash": infohash, "progress": 1.0, "files": [{"name": "Show/S01E01.mkv", "size": 10, "progress": 1.0}]}


def _disk(monkeypatch, identity):
    current = {"id": identity}
    monkeypatch.setattr(
        platform, "volume_of", lambda path: platform.Volume(current["id"], "local") if current["id"] else None
    )
    return current


def _seed(tmp_path: Path, *, recorded: str = "uuid:usb-disk", unseen: bool = True) -> Path:
    folder = tmp_path / "media"
    (folder / "Show").mkdir(parents=True)
    (folder / "Show" / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {"id": "t1", "title": "Show A", "hash": "ABC", "save_path": str(folder)}
    if unseen:
        topic["volume_unseen"] = {"folder": str(folder), "at": int(time.time())}
    state = load_state()
    state["topics"] = [topic]
    save_state(state)
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "t1": {
                    "items": {},
                    "baseline_at": "2026-09-12T20:00:00+03:00",
                    "volume": {"path": str(folder), "id": recorded},
                }
            },
        }
    )
    return folder


def _issues():
    return [
        issue
        for issue in collect(Snapshot(state=load_state(), config={}, now=time.time()))
        if issue.kind == "volume_missing"
    ]


# --- the issue -------------------------------------------------------------------------------------


def test_the_issue_appears_while_the_folder_is_off_its_disk_and_goes_when_it_is_back(tmp_path, monkeypatch):
    folder = _seed(tmp_path, unseen=False)
    disk = _disk(monkeypatch, "uuid:system-disk")
    topic = load_state()["topics"][0]
    history = load_download_history()
    reconcile_topic(topic, FakeClient(), history, "2026-09-12T21:00:00+03:00")
    assert topic["volume_unseen"]["folder"] == str(folder)
    state = load_state()
    state["topics"] = [topic]
    save_state(state)
    (issue,) = _issues()
    assert issue.id == "volume_missing:topic:t1"
    assert issue.fix is not None
    assert (issue.fix.action, issue.fix.subject) == ("accept_volume", "topic:t1")
    assert issue.message.params == {"title": "Show A", "folder": str(folder)}

    disk["id"] = "uuid:usb-disk"  # connected again
    reconcile_topic(topic, FakeClient(), history, "2026-09-12T21:30:00+03:00")
    assert "volume_unseen" not in topic
    state["topics"] = [topic]
    save_state(state)
    assert _issues() == []


def test_a_paused_topic_waits_without_an_issue(tmp_path):
    _seed(tmp_path)
    state = load_state()
    state["topics"][0]["paused"] = True
    save_state(state)
    assert _issues() == []


def test_a_backup_folder_off_its_disk_is_an_issue():
    state = load_state()
    state["backup_volumes"] = {
        "night": {"path": "/mnt/backup", "id": "usb", "unseen_at": 1},
        "manual": {"path": "/x", "id": "y"},
    }
    save_state(state)
    (issue,) = _issues()
    assert issue.id == "volume_missing:backup:night"
    assert issue.message.key == "issues.volume_missing_night"


# --- what each device sees ---------------------------------------------------------------------------


def test_this_computer_gets_the_action_a_network_device_scrubbed_words_without_it(tmp_path):
    from tow.config import load_config
    from tow.web.views import attention_view

    folder = _seed(tmp_path)
    local = attention_view(load_state(), load_config(), local=True)["entries"]
    network = attention_view(load_state(), load_config(), local=False)["entries"]
    (mine,) = [entry for entry in local if entry["kind"] == "volume_missing"]
    (theirs,) = [entry for entry in network if entry["kind"] == "volume_missing"]
    assert mine["fix"] == "accept_volume:topic:t1"
    assert str(folder) in mine["text"]
    assert mine["href"].startswith("/volumes?")
    assert theirs["fix"] == ""
    assert str(folder) not in theirs["text"]
    assert "Show A" in theirs["text"]
    assert theirs["href"].startswith("/volumes#")


def test_the_page_offers_the_action_only_on_this_computer(tmp_path, monkeypatch):
    from tow.auth import SESSION_COOKIE, issue_session, lan_password_record, lan_password_session_key
    from tow.web import app

    folder = _seed(tmp_path)
    page = TestClient(app, headers=ORIGIN).get("/volumes")
    assert page.status_code == 200
    assert t("volumes.accept", "ru") in page.text
    assert str(folder) in page.text

    open_network(monkeypatch)
    record = lan_password_record("synthetic-password-long-enough")
    from tow.store import save_secrets

    save_secrets({"lan_auth": record})
    lan = TestClient(
        app,
        base_url="http://192.168.1.2",
        client=("192.168.1.3", 50000),
        cookies={SESSION_COOKIE: issue_session(lan_password_session_key(record))},
    )
    seen = lan.get("/volumes")
    assert seen.status_code == 200
    assert t("volumes.accept", "ru") not in seen.text
    assert 'action="/volumes/accept"' not in seen.text
    assert str(folder) not in seen.text
    accepted = []
    monkeypatch.setattr("tow.web.services.accept_volume", lambda subject: accepted.append(subject) or "accepted")
    refused = lan.post(
        "/volumes/accept",
        data={"subject": "topic:t1"},
        headers={"Origin": "http://192.168.1.2"},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert flash_of(refused.headers["location"]) == t("volumes.local_only", "ru")
    assert accepted == []


@pytest.mark.parametrize("origin", [None, "http://evil.example"])
def test_an_accept_from_another_origin_is_refused(tmp_path, monkeypatch, origin):
    from tow.web import app

    _seed(tmp_path)
    accepted = []
    monkeypatch.setattr("tow.web.services.accept_volume", lambda subject: accepted.append(subject) or "accepted")
    headers = {"Origin": origin} if origin else {}
    response = TestClient(app).post(
        "/volumes/accept", data={"subject": "topic:t1"}, headers=headers, follow_redirects=False
    )
    assert response.status_code == 403
    assert accepted == []


# --- accepting ---------------------------------------------------------------------------------------


def _run_check_with(monkeypatch, client):
    """The one-topic client-only check accept runs, with ``client`` as the topic's client."""
    from tow.check import reconcile as check_reconcile

    seen = {}

    def run_check(**kwargs):
        seen.update(kwargs)
        state = load_state()
        history = load_download_history()
        topic = state["topics"][0]
        reconcile_topic(topic, client, history, "2026-09-12T22:00:00+03:00", rebind_volume=kwargs["rebind_volumes"])
        save_state(state)
        save_download_history(history)
        return {"results": []}

    monkeypatch.setattr("tow.check.run_check", run_check)
    del check_reconcile
    return seen


def test_accept_binds_the_disk_a_file_of_the_topic_is_confirmed_on_now(tmp_path, monkeypatch):
    from tow.web import app

    folder = _seed(tmp_path)
    _disk(monkeypatch, "uuid:new-disk")
    seen = _run_check_with(monkeypatch, FakeClient())
    response = TestClient(app, headers=ORIGIN).post(
        "/volumes/accept", data={"subject": "topic:t1"}, follow_redirects=False
    )
    assert flash_kind(response.headers["location"]) == "ok"
    assert flash_of(response.headers["location"]) == t("volumes.accepted", "ru")
    assert seen["ids"] == ["t1"]
    assert seen["progress_only"] is True  # the client alone: no site is asked
    assert seen["rebind_volumes"] is True
    assert load_download_history()["topics"]["t1"]["volume"] == {"path": str(folder), "id": "uuid:new-disk"}
    assert "volume_unseen" not in load_state()["topics"][0]
    assert _issues() == []


def test_accept_is_refused_when_nothing_of_the_topic_is_confirmed_there(tmp_path, monkeypatch):
    from tow.web import app

    folder = _seed(tmp_path)
    (folder / "Show" / "S01E01.mkv").unlink()  # the disk below an unmounted one: nothing of ours
    _disk(monkeypatch, "uuid:system-disk")
    _run_check_with(monkeypatch, FakeClient())
    response = TestClient(app, headers=ORIGIN).post(
        "/volumes/accept", data={"subject": "topic:t1"}, follow_redirects=False
    )
    assert flash_kind(response.headers["location"]) == "err"
    assert flash_of(response.headers["location"]) == t("volumes.refused", "ru")
    assert load_download_history()["topics"]["t1"]["volume"]["id"] == "uuid:usb-disk"


@pytest.mark.parametrize("subject", ["", "topic:", "topic:nope", "backup:night", "backup:other", "elsewhere:t1"])
def test_nothing_to_accept_is_said_so(tmp_path, subject):
    from tow import volumes

    _seed(tmp_path)
    assert volumes.accept(subject) == volumes.UNKNOWN


def test_a_backup_folder_is_accepted_only_with_a_copy_of_ours_on_it(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from tow import volumes
    from tow.config import load_config, save_config
    from tow.snapshots import SnapshotError, create_snapshot

    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    night = tmp_path.parent / f"{tmp_path.name}-night"
    cfg = load_config()
    cfg["backup_dir"] = str(night)
    save_config(cfg)
    disk = _disk(monkeypatch, "uuid:old-disk")
    create_snapshot()
    disk["id"] = "uuid:new-disk"
    with pytest.raises(SnapshotError):
        create_snapshot()
    assert _issues()[0].id == "volume_missing:backup:night"
    for copy in night.iterdir():  # an empty new disk: nothing of ours there
        shutil.rmtree(copy)
    assert volumes.accept("backup:night") == volumes.REFUSED
    assert load_state()["backup_volumes"]["night"]["id"] == "uuid:old-disk"
    disk["id"] = "uuid:old-disk"
    create_snapshot()  # the old disk is back: a copy is written there again
    disk["id"] = "uuid:new-disk"  # the copies were moved to a new disk mounted at the same place
    with pytest.raises(SnapshotError):
        create_snapshot()
    assert volumes.accept("backup:night") == volumes.ACCEPTED
    assert load_state()["backup_volumes"]["night"]["id"] == "uuid:new-disk"
    assert "unseen_at" not in load_state()["backup_volumes"]["night"]
    assert _issues() == []
    create_snapshot()  # copies go on


# --- a positive identity, or none (1.33.1 review) ------------------------------------------------------


def test_a_record_of_an_earlier_shape_is_off_its_disk_until_accepted_again(tmp_path, monkeypatch):
    """1.33.1's development builds recorded a type and mount point ("exfat /mnt/disk"), which
    another disk of that type at that place matched. Such a record matches no volume now: the
    folder is in Needs attention, and the owner's accept binds the disk's own identity."""
    from tow import volumes

    folder = _seed(tmp_path, recorded="exfat /mnt/disk", unseen=False)
    _disk(monkeypatch, "uuid:usb-disk")
    topic = load_state()["topics"][0]
    history = load_download_history()
    reconcile_topic(topic, FakeClient(), history, "2026-09-12T21:00:00+03:00")
    assert topic["volume_unseen"]["folder"] == str(folder)
    assert history["topics"]["t1"]["volume"]["id"] == "exfat /mnt/disk"  # never silently rebound
    state = load_state()
    state["topics"] = [topic]
    save_state(state)
    save_download_history(history)
    (issue,) = _issues()
    assert issue.fix is not None
    assert issue.fix.action == "accept_volume"

    _run_check_with(monkeypatch, FakeClient())
    assert volumes.accept("topic:t1") == volumes.ACCEPTED
    assert load_download_history()["topics"]["t1"]["volume"] == {"path": str(folder), "id": "uuid:usb-disk"}
    assert _issues() == []


def test_a_disk_without_an_identity_is_never_seen_nor_accepted(tmp_path, monkeypatch):
    """A file system the system cannot name (a tmpfs, a failed lookup): its files are not
    confirmed nor called missing, and accept binds nothing."""
    from tow import volumes

    _seed(tmp_path, unseen=False)
    monkeypatch.setattr(platform, "volume_of", lambda path: platform.Volume(None, "local"))
    topic = load_state()["topics"][0]
    history = load_download_history()
    reconcile_topic(topic, FakeClient(), history, "2026-09-12T21:00:00+03:00")
    assert "volume_unseen" in topic
    assert {item["status"] for item in history["topics"]["t1"]["items"].values()} == {"unverified"}
    state = load_state()
    state["topics"] = [topic]
    save_state(state)
    save_download_history(history)
    _run_check_with(monkeypatch, FakeClient())
    assert volumes.accept("topic:t1") == volumes.REFUSED
    assert load_download_history()["topics"]["t1"]["volume"]["id"] == "uuid:usb-disk"
