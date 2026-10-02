"""Every kind of undo: what its record must hold, what it restores, what it is called.

A kind only computes the stores' new contents (and does an outside effect it cannot do later,
such as moving a torrent back); ``tow.undo.engine`` reads, writes, cleans up and reports.
"""

from __future__ import annotations

import copy
from typing import Any, cast

from tow import access
from tow.config import flash_ttl, interval_sec_of, load_config
from tow.log import error_fields, log_event
from tow.store import load_secrets
from tow.undo import snapshots
from tow.undo.engine import KINDS, Context, Kind, Outcome, Refused, Restore, t
from tow.undo.records import (
    AccessChanged,
    ClientsChanged,
    SettingsChanged,
    SiteChanged,
    TopicAdded,
    TopicDeleted,
    TopicEdited,
)

# Topic fields the edit form changes; undoing an edit restores exactly these.
TOPIC_EDIT_FIELDS = ("title", "url", "client_id", "selection", "tracking_mode", "save_path")


def _short_title(record: dict[str, Any]) -> str:
    item = record.get("item")
    item = item if isinstance(item, dict) else {}
    return str(item.get("tracker_title") or item.get("title") or "").split(" / ")[0][:40]


# --------------------------------------------------------------------------- topics


def _topic_deleted(ctx: Context) -> Restore:
    record = cast(TopicDeleted, ctx.record)
    topics = ctx.state.setdefault("topics", [])
    item = record["item"]
    if not any(str(topic.get("id")) == str(item.get("id")) for topic in topics):
        index = record.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index <= len(topics):
            topics.insert(index, item)  # back where it was, not at the end
        else:
            topics.append(item)
    return Restore(Outcome("web.undo.topic_done"))


def _move_back(item: dict[str, Any], current: dict[str, Any]) -> bool:
    """The torrent goes back to the folder it was in before the edit; True when it was moved.

    The client move cannot be part of the transaction: it happens first, and a client that does
    not confirm it refuses the whole undo (nothing is written)."""
    from tow.check import await_relocation, client_owned_by_tow
    from tow.clock import iso_now
    from tow.folders import paths_equal

    old_path = str(item.get("save_path") or "")
    current_path = str(current.get("save_path") or "")
    infohash = str(current.get("hash") or item.get("hash") or "")
    if not (infohash and old_path and current_path and not paths_equal(old_path, current_path)):
        return False
    try:
        from tow.clients.factory import from_secrets

        adapter = from_secrets(load_config(), load_secrets(), str(current.get("client_id") or "") or None)
        if not client_owned_by_tow(adapter, infohash):
            raise RuntimeError(t("web.topics.not_owned"))
        adapter.set_location(infohash, old_path)
        outcome = await_relocation(adapter, infohash, old_path)
        if outcome == "failed":
            raise RuntimeError(t("web.topics.move_back_unconfirmed"))
    except Exception as exc:
        fields = error_fields(exc)
        log_event("qbit_move_undo_fail", topic=item.get("id"), hash=infohash, path=old_path, **fields, how="manual")
        raise Refused(Outcome("web.undo.relocate_failed", "err")) from exc
    if outcome == "moving":
        item["move_pending"] = {"from": current_path, "to": old_path, "since": iso_now()}
    else:
        item.pop("move_pending", None)
    status = "succeeded" if outcome == "done" else "moving"
    log_event("qbit_move_undo", topic=item.get("id"), hash=infohash, path=old_path, status=status, how="manual")
    return True


def _topic_edited(ctx: Context) -> Restore:
    item = copy.deepcopy(cast(TopicEdited, ctx.record)["item"])
    topics = ctx.state.setdefault("topics", [])
    current = next((topic for topic in topics if str(topic.get("id")) == str(item.get("id"))), None)
    if current is not None:
        relocated = _move_back(item, current)
        # Only what the edit form changed comes back. Everything written since the edit - check
        # status (last_ok/last_error/...), hashes, selection read-back, progress - stays.
        restored = copy.deepcopy(current)
        for key in TOPIC_EDIT_FIELDS:
            if key in item:
                restored[key] = copy.deepcopy(item[key])
            else:
                restored.pop(key, None)
        if relocated:
            if "move_pending" in item:
                restored["move_pending"] = item["move_pending"]
            else:
                restored.pop("move_pending", None)
        selection_changed = item.get("selection") != current.get("selection")
        tracking_changed = str(item.get("tracking_mode") or "watch") != str(current.get("tracking_mode") or "watch")
        restored["selection_dirty"] = bool(current.get("hash")) and (
            selection_changed or bool(current.get("selection_dirty"))
        )
        if selection_changed or tracking_changed:
            restored["once_done"] = False
        topics[topics.index(current)] = restored
    return Restore(Outcome("web.undo.topic_put_done"))


def _topic_added(ctx: Context) -> Restore:
    added = str(cast(TopicAdded, ctx.record)["id"])
    ctx.state["topics"] = [topic for topic in ctx.state.get("topics") or [] if str(topic.get("id")) != added]
    return Restore(Outcome("web.undo.topic_add_done"))


# --------------------------------------------------------------------------- sites


def _site(ctx: Context) -> Restore:
    record = cast(SiteChanged, ctx.record)
    name = str(record["name"])
    renamed = str(record.get("renamed_to") or "")
    renamed = renamed if renamed != name else ""
    config = load_config()
    trackers = config.setdefault("trackers", {})
    if renamed:
        trackers.pop(renamed, None)
    trackers[name] = copy.deepcopy(record["spec"])
    mirrors = ctx.state.setdefault("mirrors", {})
    if renamed:
        mirrors.pop(renamed, None)
    if record.get("mirror_present"):
        mirrors[name] = copy.deepcopy(record.get("mirror"))
    else:
        mirrors.pop(name, None)
    secrets = None
    if ctx.snapshot is not None:
        secrets = copy.deepcopy(load_secrets())
        logins = secrets.setdefault("trackers", {})
        if renamed:
            logins.pop(renamed, None)
        if ctx.snapshot["present"]:
            logins[name] = copy.deepcopy(ctx.snapshot["value"])
        else:
            logins.pop(name, None)
    return Restore(Outcome("web.undo.site_done", "ok", "/sites"), config=config, secrets=secrets)


# --------------------------------------------------------------------------- settings


def _settings(ctx: Context) -> Restore:
    assert ctx.snapshot is not None  # a settings record always has one (``valid``)
    record = cast(SettingsChanged, ctx.record)
    secrets = snapshots.restore_scoped(load_secrets(), ctx.snapshot, record.get("secret_scope"))
    config = load_config()
    old_sec = interval_sec_of(config)
    old_ttl = flash_ttl(config.get("flash_ttl_sec"))
    target_sec = int(record.get("interval_sec") or old_sec)
    ttl = record.get("flash_ttl_sec")
    target_ttl = int(ttl) if ttl is not None else old_ttl
    # A changed check interval needs nothing outside the config: `tow run` reads it from there.
    return Restore(
        Outcome("web.undo.settings_done", "ok", "/settings"),
        secrets=secrets,
        interval_sec=target_sec if target_sec != old_sec else None,
        flash_ttl_sec=target_ttl if target_ttl != old_ttl else None,
        failed=Outcome("web.undo.apply_failed", "err", "/settings?open=clients"),
    )


def _session_key(secrets: dict[str, Any]) -> str | None:
    """The key network sessions are signed with for this password (None: no usable password)."""
    from tow.auth import lan_password_session_key

    record = access.password_record(secrets)
    return lan_password_session_key(record) if record is not None else None


_ACCESS = "/settings?open=access"


def _access(ctx: Context) -> Restore:
    """Network access, or the password (a record stamped by the password card has its snapshot).

    A signed-in device on the network is the owner and may undo a password change; turning
    network access on or off stays with the computer running TOW (as the change itself), so an
    undo that would switch it, or leave the network open without a password, is refused there.
    A password that comes back moves the session epoch: no session from before the undo - made
    with the old password or with the new one - is valid again.
    """
    current = load_config()
    config = copy.deepcopy(current)
    record = cast(AccessChanged, ctx.record)
    config["bind"] = str(record["old_bind"])
    config["allow_lan"] = bool(record.get("old_allow_lan"))
    secrets_now = load_secrets()
    secrets = None
    if ctx.snapshot is not None:
        secrets = snapshots.restore_scoped(secrets_now, ctx.snapshot, record.get("secret_scope"))
    switches_access = config["bind"] != str(current.get("bind") or "") or config["allow_lan"] != bool(
        current.get("allow_lan")
    )
    open_without_password = config["allow_lan"] and _session_key(secrets_now if secrets is None else secrets) is None
    if not ctx.local and (switches_access or open_without_password):
        raise Refused(Outcome("web.undo.access_local_only", "err", _ACCESS))
    event: dict[str, Any] = {"bind": config["bind"], "allow_lan": config["allow_lan"]}
    done = Outcome("web.undo.access_done", "ok", _ACCESS)
    after_commit = None
    if secrets is not None and secrets.get(access.RECORD_KEY) != secrets_now.get(access.RECORD_KEY):
        if _session_key(secrets) == _session_key(secrets_now):
            event["password"] = "hint"
            done = Outcome("web.undo.hint_done", "ok", _ACCESS)
        else:
            restored = _session_key(secrets) is not None
            event["password"] = "restored" if restored else "removed"
            message = "web.undo.password_done" if restored else "web.undo.password_removed"
            done = Outcome(message, "ok", _ACCESS, signed_out=True)
            after_commit = access.sign_out_everywhere
    return Restore(
        done,
        config=config,
        secrets=secrets,
        event=("settings_access_undo", event),
        after_commit=after_commit,
        after_commit_failed="web.undo.sign_out_failed",
    )


def _clients(ctx: Context) -> Restore:
    assert ctx.snapshot is not None
    secrets = snapshots.restore_scoped(load_secrets(), ctx.snapshot, ["clients"])
    before = cast(ClientsChanged, ctx.record)["config_before"]
    config = load_config()
    for key in ("client", "clients"):
        if before.get(key) is None:
            config.pop(key, None)
        else:
            config[key] = before[key]
    return Restore(
        Outcome("web.undo.clients_done", "ok", "/settings?open=clients"),
        config=config,
        secrets=secrets,
        event=("settings_clients_undo", {}),
    )


# --------------------------------------------------------------------------- the registry


def _store_snapshot(value: object, _record: dict[str, Any]) -> bool:
    return snapshots.is_store_snapshot(value)


def _site_snapshot(value: object, record: dict[str, Any]) -> bool:
    return snapshots.is_site_snapshot(value, str(record.get("name")))


def _titled(record: dict[str, Any], key: str, plain: str) -> str:
    title = _short_title(record)
    return t(key, title=title) if title else t(plain)


KINDS.update(
    {
        "topic": Kind(
            _topic_deleted,
            valid=lambda r: isinstance(r.get("item"), dict),
            label=lambda r: _titled(r, "web.undo.topic", "web.undo.topic_plain"),
        ),
        "topic_put": Kind(
            _topic_edited,
            valid=lambda r: isinstance(r.get("item"), dict),
            label=lambda r: _titled(r, "web.undo.topic_put", "web.undo.topic_put_plain"),
        ),
        "topic_add": Kind(_topic_added, valid=lambda r: bool(r.get("id")), label=lambda _r: t("web.undo.topic_add")),
        "site": Kind(
            _site,
            valid=lambda r: bool(r.get("name")) and isinstance(r.get("spec"), dict),
            label=lambda r: t("web.undo.site", name=r["name"]) if r.get("name") else t("web.undo.site_plain"),
            target="/sites",
            snapshot=_site_snapshot,
        ),
        "settings": Kind(
            _settings,
            valid=lambda r: bool(r.get("secrets_undo_ref")),
            label=lambda _r: t("web.undo.settings"),
            target="/settings",
            snapshot=_store_snapshot,
            secrets_failed=Outcome("web.undo.secrets_failed", "err", "/settings?open=clients"),
        ),
        "settings_access": Kind(
            _access,
            valid=lambda r: "old_bind" in r,
            label=lambda r: t(
                "web.undo.password" if r.get("secret_scope") == [access.RECORD_KEY] else "web.undo.settings_access"
            ),
            target="/settings?open=access",
            snapshot=_store_snapshot,
            secrets_failed=Outcome("web.undo.password_failed", "err", "/settings?open=access"),
            failed="web.undo.access_failed",
        ),
        "settings_clients": Kind(
            _clients,
            valid=lambda r: bool(r.get("secrets_undo_ref")) and isinstance(r.get("config_before"), dict),
            label=lambda _r: t("web.undo.settings_clients"),
            target="/settings?open=clients",
            snapshot=_store_snapshot,
            secrets_failed=Outcome("web.undo.clients_failed", "err", "/settings?open=clients"),
            failed="web.undo.clients_not_applied",
        ),
    }
)
