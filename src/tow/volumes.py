"""The owner's "it is the same disk - accept": a folder whose recorded volume is missing or another
one (Needs attention, tow.issues ``volume_missing``) is bound to the volume it is on now - only
when what TOW keeps there is found on it now, the rule ``tow check --rebind-volumes`` follows.

- A topic: a client-only check of that topic binds its folder again from a file of the topic
  confirmed on disk in full now (tow.progress.reconcile); with none confirmed it is refused.
- A backup folder: a night copy signed by this install, or a restore point this install can open,
  must be in it now; then its volume is recorded anew and the note goes.

Nothing else binds a folder anew: a mismatch alone never does (tow.folders.recorded_volume).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

ACCEPTED = "accepted"
REFUSED = "refused"  # nothing of ours is found there now
BUSY = "busy"  # an applying check runs: try again in a moment
UNKNOWN = "unknown"  # no such topic or folder, or nothing to accept for it


def accept(subject: str) -> str:
    """Bind ``topic:<id>`` or ``backup:night`` / ``backup:manual`` to the volume it is on now."""
    kind, _, name = str(subject or "").partition(":")
    if kind == "topic" and name:
        return _accept_topic(name)
    if kind == "backup" and name in {"night", "manual"}:
        return _accept_backup(name)
    return UNKNOWN


def _topic(state: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    return next((t for t in state.get("topics") or [] if isinstance(t, dict) and str(t.get("id")) == topic_id), None)


def _accept_topic(topic_id: str) -> str:
    from tow.check import run_check
    from tow.folders import recorded_volume, volume_at
    from tow.storage.locks import CheckBusyError
    from tow.store import load_download_history, load_state

    topic = _topic(load_state(), topic_id)
    if topic is None or not isinstance(topic.get("volume_unseen"), dict):
        return UNKNOWN
    try:
        run_check(
            apply=True,
            notify=False,
            ids=[topic_id],
            how="progress",
            progress_only=True,
            wait=False,
            rebind_volumes=True,
        )
    except CheckBusyError:
        return BUSY
    topic = _topic(load_state(), topic_id)
    record = ((load_download_history().get("topics") or {}).get(topic_id) or {}).get("volume")
    if topic is None or topic.get("volume_unseen") or not isinstance(record, dict):
        return REFUSED
    expected = recorded_volume(record, str(record.get("path") or ""))
    return ACCEPTED if expected and volume_at(str(record["path"])) == expected else REFUSED


def _ours_in(name: str, root: Path) -> bool:
    """Something this install wrote is in ``root`` now: a signed night copy, or a restore point
    its key opens."""
    if name == "night":
        from tow.snapshots.location import snapshot_folders
        from tow.snapshots.manifest import owned_manifest, signing_key

        key = signing_key()
        return any(owned_manifest(folder, key) is not None for folder in snapshot_folders(root))
    from tow import restore_points

    for point in restore_points.list_restore_points():
        try:
            restore_points.check_restore_point(str(point["id"]))  # opened with this install's key
        except OSError, restore_points.RestorePointError:
            continue
        return True
    return False


def _accept_backup(name: str) -> str:
    from tow import locations
    from tow.folders import volume_at
    from tow.store import load_state, persistence_lock, save_state

    with persistence_lock():
        state = load_state()
        volumes = state.get(locations.VOLUMES_KEY)
        entry = volumes.get(name) if isinstance(volumes, dict) else None
        if not isinstance(entry, dict) or "unseen_at" not in entry:
            return UNKNOWN
        try:
            if name == "night":
                from tow.snapshots.location import backup_root

                root = backup_root()
            else:
                from tow.restore_points import restore_points_dir

                root = restore_points_dir()
            identity = volume_at(str(root))
            if not identity or not root.is_dir() or not _ours_in(name, root):
                return REFUSED
        except Exception:  # noqa: BLE001 - settings, folder or key trouble: nothing is accepted, the owner is told
            return REFUSED
        assert isinstance(volumes, dict)
        volumes[name] = {"path": str(root), "id": identity}
        save_state(state)
    return ACCEPTED
