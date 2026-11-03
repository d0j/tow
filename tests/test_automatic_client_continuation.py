"""Recovery waits resume safely; a reserved native command is never sent twice."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_client_verification import H
from test_client_verification import _run as verification_run
from test_client_verification import world as verification_world
from test_file_repair import advance
from test_file_repair import world as repair_world
from test_file_repair_start_confirmation import delayed_start
from test_wait_for_space import GIB, NEW, _check, _space_pass
from test_wait_for_space import world as space_world

from tow import file_repair, platform, repair_authorization
from tow.check import repair, verification
from tow.store import load_state, save_state
from tow.supervisor import _facts


@pytest.fixture
def waiting_space(monkeypatch, tmp_path):
    return space_world.__wrapped__(monkeypatch, tmp_path)


@pytest.fixture
def verifying(monkeypatch, tmp_path):
    return verification_world.__wrapped__(monkeypatch, tmp_path)


@pytest.mark.parametrize("condition", ["space", "volume"])
def test_missing_resource_keeps_the_same_authorized_repair_and_continues_when_it_returns(
    tmp_path, monkeypatch, condition
):
    client, clock = repair_world(tmp_path, monkeypatch)
    requested = file_repair.request("synthetic")
    if condition == "space":
        monkeypatch.setattr(repair.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    else:
        monkeypatch.setattr(platform, "volume_of", lambda path: platform.Volume("uuid:other-disk", "local"))
    assert advance(client)[1]["status"] == "repair_pending"
    waiting = load_state()["topics"][0]["repair_pending"]
    assert waiting["retry_error"]["code"] == f"repair.waiting_{condition}"
    assert not waiting.get("blocked")
    assert client.calls == []
    assert _facts()["space_waiting"]
    assert advance(client)[2] == []  # no extra attempt, notification, or command before next_at
    monkeypatch.setattr(repair.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**12))
    monkeypatch.setattr(platform, "volume_of", lambda path: platform.Volume("uuid:repair-disk", "local"))
    clock["now"] = waiting["next_at"]
    assert advance(client)[1]["repair_phase"] == "checking"
    current = load_state()["topics"][0]["repair_pending"]
    assert current["id"] == requested["id"]
    assert current["deadline"] == requested["deadline"]
    assert not current.get("retry_error")
    assert client.calls == ["check"]


def test_temporary_client_outage_is_bounded_by_original_deadline_not_three_observations(tmp_path, monkeypatch):
    client, clock = repair_world(tmp_path, monkeypatch)
    requested = file_repair.request("synthetic")
    inspect = client.inspect_torrent

    def offline(h):
        raise OSError("synthetic temporary client outage")

    monkeypatch.setattr(client, "inspect_torrent", offline)
    for _ in range(5):
        assert advance(client)[1]["status"] == "repair_pending"
        clock["now"] = load_state()["topics"][0]["repair_pending"]["next_at"]
    waiting = load_state()["topics"][0]["repair_pending"]
    assert waiting["attempts"] == file_repair.MAX_ATTEMPTS
    assert waiting["deadline"] == requested["deadline"]
    assert client.calls == []
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    advance(client)
    assert client.calls == ["check"]
    assert load_state()["topics"][0]["repair_pending"]["attempts"] == 0


def test_healthy_observations_reset_consecutive_failures_without_rearming_the_start(tmp_path, monkeypatch):
    client, clock, requested = delayed_start(tmp_path, monkeypatch)
    client.info["state"] = "downloading"
    advance(client)
    inspect = client.inspect_torrent
    for _ in range(4):
        monkeypatch.setattr(client, "inspect_torrent", lambda h: (_ for _ in ()).throw(OSError("temporary")))
        advance(client)
        waiting = load_state()["topics"][0]["repair_pending"]
        assert waiting["attempts"] == 1
        clock["now"] = waiting["next_at"]
        monkeypatch.setattr(client, "inspect_torrent", inspect)
        advance(client)
        current = load_state()["topics"][0]["repair_pending"]
        assert current["attempts"] == 0
        assert current["deadline"] == requested["deadline"]
    assert client.calls == ["resume"]


@pytest.mark.parametrize("state", ["checkingDL", "checkingResumeData", "allocating"])
def test_known_native_preparation_waits_without_false_failure_or_a_second_start(tmp_path, monkeypatch, state):
    client, clock, requested = delayed_start(tmp_path, monkeypatch)
    client.info["state"] = state
    clock["now"] += 600
    assert advance(client)[1]["status"] == "repair_pending"
    client.info["state"] = "queuedDL"
    assert advance(client)[1]["repair_phase"] == "downloading"
    assert client.calls == ["resume"]
    assert load_state()["topics"][0]["repair_pending"]["deadline"] == requested["deadline"]


def test_native_running_after_unconfirmed_start_timeout_is_observed_without_retrying(tmp_path, monkeypatch):
    client, clock, requested = delayed_start(tmp_path, monkeypatch)
    clock["now"] += repair.CONFIRM_SEC
    assert advance(client)[1]["status"] == "failed"
    assert file_repair.maintenance_pending(load_state()["topics"][0])
    client.info["state"] = "downloading"
    assert advance(client)[1]["repair_phase"] == "downloading"
    assert client.calls == ["resume"]
    assert load_state()["topics"][0]["repair_pending"]["deadline"] == requested["deadline"]
    client.info["state"] = "stoppedDL"
    advance(client)
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.client_stopped"
    assert not file_repair.maintenance_pending(load_state()["topics"][0])
    assert client.calls == ["resume"]


def test_pending_resource_never_extends_original_authorization(tmp_path, monkeypatch):
    client, clock = repair_world(tmp_path, monkeypatch)
    requested = file_repair.request("synthetic")
    monkeypatch.setattr(repair.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    advance(client)
    clock["now"] = requested["deadline"]
    advance(client)
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.deadline"
    assert not _facts()["space_waiting"]
    assert client.calls == []


def test_space_start_reservation_is_durable_before_rpc_and_lost_reply_cannot_override_later_owner_stop(
    waiting_space, monkeypatch
):
    world = waiting_space
    _check()
    world.disk["free"] = 60 * GIB
    native_start = world.client.start_owned_torrent

    def accepted_lost_reply(h):
        saved = load_state()["topics"][0]
        assert not saved.get("waiting_space")
        assert saved["verification_pending"]["resume_attempted"] is True
        native_start(h)
        raise OSError("reply lost after the native start")

    monkeypatch.setattr(world.client, "start_owned_torrent", accepted_lost_reply)
    _space_pass()
    assert world.client.starts == [NEW]
    world.client.torrents[NEW]["state"] = "stoppedDL"
    monkeypatch.setattr(world.client, "start_owned_torrent", native_start)
    _space_pass()
    _space_pass()
    assert world.client.starts == [NEW]
    assert load_state()["topics"][0]["verification_pending"]["resume_attempted"] is True


def test_late_native_start_of_a_space_wait_clears_stale_failure_without_a_second_command(waiting_space, monkeypatch):
    world = waiting_space
    _check()
    world.disk["free"] = 60 * GIB
    original = world.client.start_owned_torrent

    def lost(h):
        original(h)
        raise OSError("reply lost")

    monkeypatch.setattr(world.client, "start_owned_torrent", lost)
    _space_pass()
    monkeypatch.setattr(world.client, "start_owned_torrent", original)
    _space_pass()
    topic = load_state()["topics"][0]
    assert not topic.get("waiting_space")
    assert not topic.get("verification_pending")
    assert not topic.get("last_error")
    assert world.client.starts == [NEW]


def test_verification_rpc_has_a_durable_one_shot_reservation(verifying, monkeypatch):
    client, _tracker, _sent = verifying
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    original = client._start

    def start(h):
        assert load_state()["topics"][0]["verification_pending"]["resume_attempted"] is True
        original(h)

    monkeypatch.setattr(client, "_start", start)
    client.calls.clear()
    assert verification_run(space_only=True)["ok"]
    assert [call[0] for call in client.calls] == ["start", "labels"]


def test_native_error_recovery_finishes_verification_without_starting_or_reselecting(verifying):
    client, _tracker, _sent = verifying
    verification_run()
    client.checking = False
    client.torrents[H]["state"] = "error"
    verification_run(space_only=True)
    assert load_state()["topics"][0]["verification_pending"]["blocked"] == "client.managed.error_state"
    client.torrents[H].update(state="downloading", progress=0.3)
    client.calls.clear()
    assert verification_run(space_only=True)["ok"]
    topic = load_state()["topics"][0]
    assert not topic.get("verification_pending")
    assert not topic.get("last_error")
    assert [call[0] for call in client.calls] == ["labels"]


@pytest.mark.parametrize("change", [{"paused": True}, {"file_observation_revision": "changed-endpoint"}])
def test_concurrent_owner_context_change_before_verification_start_sends_no_command(verifying, monkeypatch, change):
    client, _tracker, _sent = verifying
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    verify = client._verify_receipt

    def owner_changed(*args, **kwargs):
        result = verify(*args, **kwargs)
        disk = load_state()
        disk["topics"][0].update(change)
        save_state(disk)
        return result

    monkeypatch.setattr(client, "_verify_receipt", owner_changed)
    client.calls.clear()
    verification_run(space_only=True)
    assert client.calls == []
    assert all(load_state()["topics"][0].get(key) == value for key, value in change.items())


class SimulatedExit(BaseException):
    """An interrupted process does not get the check's normal exception/commit cleanup."""


def test_verification_restart_after_rpc_boundary_never_replays_a_start_or_native_owner_stop(verifying, monkeypatch):
    client, _tracker, _sent = verifying
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    original = client._start

    def accepted_then_exit(h):
        assert load_state()["topics"][0]["verification_pending"]["resume_attempted"] is True
        original(h)
        raise SimulatedExit

    monkeypatch.setattr(client, "_start", accepted_then_exit)
    client.calls.clear()
    with pytest.raises(SimulatedExit):
        verification_run(space_only=True)
    assert [call[0] for call in client.calls] == ["start"]
    assert load_state()["topics"][0]["verification_pending"]["resume_attempted"] is True
    client.torrents[H]["state"] = "stoppedDL"  # the owner stopped it after the accepted command
    monkeypatch.setattr(client, "_start", original)
    verification_run(space_only=True)
    verification_run(space_only=True)
    assert [call[0] for call in client.calls] == ["start"]


def test_space_restart_after_rpc_boundary_keeps_durable_start_fence(waiting_space, monkeypatch):
    world = waiting_space
    _check()
    world.disk["free"] = 60 * GIB
    original = world.client.start_owned_torrent

    def accepted_then_exit(h):
        original(h)
        raise SimulatedExit

    monkeypatch.setattr(world.client, "start_owned_torrent", accepted_then_exit)
    with pytest.raises(SimulatedExit):
        _space_pass()
    saved = load_state()["topics"][0]
    assert saved["verification_pending"]["resume_attempted"] is True
    assert not saved.get("waiting_space")
    world.client.torrents[NEW]["state"] = "stoppedDL"
    monkeypatch.setattr(world.client, "start_owned_torrent", original)
    _space_pass()
    _space_pass()
    assert world.client.starts == [NEW]


def test_failed_durable_verification_reservation_never_sends_start_and_can_retry_when_storage_returns(
    verifying, monkeypatch
):
    client, _tracker, _sent = verifying
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    original = verification.save_state

    def failed(_state):
        raise OSError("synthetic reservation storage failure")

    monkeypatch.setattr(verification, "save_state", failed)
    client.calls.clear()
    verification_run(space_only=True)
    assert client.calls == []
    assert not load_state()["topics"][0]["verification_pending"].get("resume_attempted")
    monkeypatch.setattr(verification, "save_state", original)
    assert verification_run(space_only=True)["ok"]
    assert [call[0] for call in client.calls] == ["start", "labels"]


def test_late_recheck_after_a_timeout_continues_without_repeating_the_check(tmp_path, monkeypatch):
    client, clock = repair_world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    client.fail_check = True
    advance(client)
    client.info["state"] = "stoppedDL"
    clock["now"] += repair.CONFIRM_SEC + 1
    advance(client)
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.dispatch_unconfirmed"
    assert file_repair.maintenance_pending(load_state()["topics"][0])
    client.info["state"] = "checkingDL"
    advance(client)
    client.finish_check()
    advance(client)
    assert load_state()["topics"][0]["repair_pending"]["phase"] == "downloading"
    assert client.calls == ["check", "resume"]


def test_late_stop_confirmation_never_repeats_stop_and_allows_only_the_unsent_check(tmp_path, monkeypatch):
    client, clock = repair_world(tmp_path, monkeypatch)
    client.info["state"] = "uploading"
    file_repair.request("synthetic")

    def asynchronous_stop(h):
        client.calls.append("stop")
        return client.inspect_torrent(h)

    monkeypatch.setattr(client, "stop_owned_torrent", asynchronous_stop)
    advance(client)
    clock["now"] += repair.CONFIRM_SEC
    advance(client)
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.dispatch_unconfirmed"
    assert file_repair.maintenance_pending(load_state()["topics"][0])
    client.info["state"] = "stoppedUP"
    advance(client)
    advance(client)
    assert client.calls == ["stop", "check"]


def test_known_remote_client_refusal_is_not_a_temporary_missing_disk(tmp_path, monkeypatch):
    client, _clock = repair_world(tmp_path, monkeypatch)
    monkeypatch.setattr(file_repair.factory, "on_this_computer", lambda *args, **kwargs: False)
    from tow.errors import TowError

    with pytest.raises(TowError) as error:
        file_repair.request("synthetic")
    assert error.value.code == "repair.remote_unavailable"
    assert not load_state()["topics"][0].get("repair_pending")
    assert client.calls == []


def test_a_selection_edit_withdrawn_during_preflight_is_not_resurrected_by_the_check_merge(verifying, monkeypatch):
    client, _tracker, _sent = verifying
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    verify = client._verify_receipt

    def withdrawn(*args, **kwargs):
        info = verify(*args, **kwargs)
        state = load_state()
        state["topics"][0]["selection"] = {"mode": "files", "value": "*e02.mkv"}
        state["topics"][0]["selection_dirty"] = True
        state["topics"][0].pop("verification_pending", None)
        save_state(state)
        return info

    monkeypatch.setattr(client, "_verify_receipt", withdrawn)
    client.calls.clear()
    verification_run(space_only=True)
    assert client.calls == []
    assert not load_state()["topics"][0].get("verification_pending")
    assert load_state()["topics"][0]["selection_dirty"] is True


def test_space_wait_does_not_transfer_authority_to_another_connection_generation(waiting_space):
    world = waiting_space
    _check()
    state = load_state()
    state["topics"][0]["file_observation_revision"] = "different-endpoint"
    save_state(state)
    world.disk["free"] = 60 * GIB
    _space_pass()
    topic = load_state()["topics"][0]
    assert topic["waiting_space"]["blocked"] == "check.selection_unconfirmed"
    assert not _facts()["space_waiting"]
    assert world.client.starts == []


def test_an_archived_undo_restored_during_preflight_cannot_be_overwritten_or_started(verifying, monkeypatch):
    client, _tracker, _sent = verifying
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    verify = client._verify_receipt

    def archive_restored(*args, **kwargs):
        info = verify(*args, **kwargs)
        state = load_state()
        repair_authorization.forget(state)
        save_state(state)
        return info

    monkeypatch.setattr(client, "_verify_receipt", archive_restored)
    client.calls.clear()
    verification_run(space_only=True)
    assert client.calls == []
    saved = load_state()["topics"][0]
    assert saved["verification_pending"]["blocked"] == "check.restored_confirmation"
    assert not saved["verification_pending"].get("resume_attempted")


@pytest.mark.parametrize("change", ["default", "disabled", "endpoint"])
def test_deferred_start_revalidates_live_default_enabled_and_adapter_config(verifying, monkeypatch, change):
    from tow.check import run as check_run
    from tow.clients import factory

    client, _tracker, _sent = verifying
    configuration = check_run.load_config()
    monkeypatch.setattr(factory, "default_client_id", lambda cfg: str(cfg["client"]["id"]))
    state = load_state()
    state["topics"][0].pop("client_id", None)  # the owner follows the configured default
    save_state(state)
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    verify = client._verify_receipt

    def changed(*args, **kwargs):
        info = verify(*args, **kwargs)
        if change == "default":
            configuration["client"]["id"] = "different-client"
        elif change == "disabled":
            configuration["client"]["enabled"] = False
        else:
            configuration["client"]["host"] = "https://different.example"
        return info

    monkeypatch.setattr(client, "_verify_receipt", changed)
    client.calls.clear()
    verification_run(space_only=True)
    assert client.calls == []
    assert not load_state()["topics"][0]["verification_pending"].get("resume_attempted")


def test_repair_disabled_during_preflight_cannot_dispatch(tmp_path, monkeypatch):
    client, _clock = repair_world(tmp_path, monkeypatch)
    cfg = file_repair.load_config()
    monkeypatch.setattr(file_repair, "load_config", lambda: cfg)
    file_repair.request("synthetic")
    inspect = client.inspect_torrent

    def disabled(h):
        info = inspect(h)
        cfg["clients"]["main"]["enabled"] = False
        return info

    monkeypatch.setattr(client, "inspect_torrent", disabled)
    advance(client)
    assert client.calls == []
    assert not load_state()["topics"][0]["repair_pending"].get("recheck_dispatched")


def test_deferred_start_revalidates_secret_endpoint_before_rpc(verifying, monkeypatch):
    from tow.check import run as check_run

    client, _tracker, _sent = verifying
    secret = {"host": "https://first.example"}
    original_read = check_run.read_secrets
    monkeypatch.setattr(check_run, "read_secrets", lambda: ({"qbittorrent": dict(secret)}, original_read()[1]))
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    verify = client._verify_receipt

    def changed(*args, **kwargs):
        info = verify(*args, **kwargs)
        secret["host"] = "https://different.example"
        return info

    monkeypatch.setattr(client, "_verify_receipt", changed)
    client.calls.clear()
    verification_run(space_only=True)
    assert client.calls == []
    assert not load_state()["topics"][0]["verification_pending"].get("resume_attempted")


@pytest.mark.parametrize("change", ["title", "default"])
def test_explicit_client_presentation_or_default_change_does_not_cancel_authorized_start(
    verifying, monkeypatch, change
):
    from tow.check import run as check_run

    client, _tracker, _sent = verifying
    cfg = check_run.load_config()
    cfg["clients"] = {
        "main": {"kind": "fake", "default": True},
        "other": {"kind": "fake", "default": False},
    }
    cfg.pop("client", None)
    verification_run()
    client.end_check(found=0.3, dropped_start=True)
    verify = client._verify_receipt

    def changed(*args, **kwargs):
        info = verify(*args, **kwargs)
        if change == "title":
            cfg["clients"]["main"]["title"] = "New display name"
        else:
            cfg["clients"]["main"]["default"] = False
            cfg["clients"]["other"]["default"] = True
        return info

    monkeypatch.setattr(client, "_verify_receipt", changed)
    client.calls.clear()
    assert verification_run(space_only=True)["ok"]
    assert [call[0] for call in client.calls] == ["start", "labels"]
    assert not load_state()["topics"][0].get("verification_pending")


@pytest.mark.parametrize("status", [403, 429, 500])
def test_real_qbittorrent_http_exceptions_distinguish_required_action_from_transport_wait(
    tmp_path, monkeypatch, status
):
    import qbittorrentapi

    client, _clock = repair_world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    error = {403: qbittorrentapi.Forbidden403Error, 500: qbittorrentapi.HTTP500Error}.get(
        status, qbittorrentapi.HTTPError
    )

    def failed(h):
        from requests import Response

        response = Response()
        response.status_code = status
        raise error("synthetic HTTP refusal", response=response)

    monkeypatch.setattr(client, "inspect_torrent", failed)
    row = advance(client)[1]
    pending = load_state()["topics"][0]["repair_pending"]
    if status == 403:
        assert row["status"] == "failed"
        assert pending["blocked"] == "client.managed.http_error"
        assert not file_repair.continuation_pending(load_state()["topics"][0])  # read-only proof cannot resume it
    else:
        assert row["status"] == "repair_pending"
        assert pending["retry_error"]["code"] == "repair.waiting_client"
        assert file_repair.maintenance_pending(load_state()["topics"][0])
    assert client.calls == []


@pytest.mark.parametrize("typed", [False, True])
def test_server_retry_after_is_obeyed_without_extending_authorization_or_sending_another_command(
    tmp_path, monkeypatch, typed
):
    import qbittorrentapi
    from requests import Response

    from tow.errors import TowError

    client, clock = repair_world(tmp_path, monkeypatch)
    requested = file_repair.request("synthetic")
    inspect = client.inspect_torrent
    response = Response()
    response.status_code = 429
    response.headers["Retry-After"] = "86400"

    def refused(h):
        if typed:
            raise TowError("client.managed.http_error", code=503, retry_after=86400)
        raise qbittorrentapi.HTTPError("synthetic busy response", response=response)

    monkeypatch.setattr(client, "inspect_torrent", refused)
    advance(client)
    waiting = load_state()["topics"][0]["repair_pending"]
    assert waiting["next_at"] >= clock["now"] + 86400
    assert waiting["deadline"] == requested["deadline"]
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    assert advance(client)[2] == []
    assert client.calls == []
    clock["now"] = requested["deadline"]
    advance(client)
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.deadline"
    assert client.calls == []


@pytest.mark.parametrize("retry", [{"code": []}, {"code": "repair.waiting_client", "params": []}])
def test_corrupt_retry_metadata_is_not_permission_to_continue(tmp_path, monkeypatch, retry):
    client, _clock = repair_world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    state = load_state()
    state["topics"][0]["repair_pending"]["retry_error"] = retry
    save_state(state)
    assert not file_repair.receipt_of(state["topics"][0])
    assert not file_repair.maintenance_pending(state["topics"][0])
    assert advance(client)[1]["status"] == "failed"
    assert client.calls == []


def test_permanent_configuration_failure_does_not_become_a_transient_legacy_observer(tmp_path, monkeypatch):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch)
    state = load_state()
    run = SimpleNamespace(
        apply=True,
        how="progress",
        state=state,
        remote_clients=frozenset(),
        get_client_for=lambda topic: ("main", None),
        client_errors={"main": ValueError("synthetic invalid client settings")},
        record=lambda *args, **kwargs: None,
    )
    repair.recheck(state["topics"][0], run, {})
    pending = load_state()["topics"][0]["repair_pending"]
    assert pending["blocked"] == "repair.failed"
    assert pending["owner_required"] is True
    assert not file_repair.maintenance_pending(load_state()["topics"][0])
    assert client.calls == ["resume"]
