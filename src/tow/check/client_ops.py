"""What a check asks of a torrent client: the torrent's hash identity, TOW's ownership mark,
read-back confirmation, relocation, a live previous revision and other topics' claims on the
same torrent; and the clients one run talks to (``ClientPool``). The web pages and undo use the
ownership and relocation checks too."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tow.clients import factory as client_factory
from tow.clients.spec import TorrentClientAdapter
from tow.errors import Msg, TowError
from tow.events import new_operation_id
from tow.folders import paths_equal
from tow.jsonish import as_dict
from tow.log import error_fields
from tow.notify import NotificationBatch
from tow.records import Topic, shown_title, topics_of
from tow.torrent import parse_magnet_hashes, windows_path_key


def _client_info_matches_hash(info: dict[str, Any], infohash: str) -> bool:
    wanted = str(infohash or "").strip().upper()
    if not wanted:
        return False
    primary = str(info.get("hash") or "").strip().upper()
    v1 = str(info.get("infohash_v1") or "").strip().upper()
    v2 = str(info.get("infohash_v2") or "").strip().upper()
    identities = {primary, v1, v2, v2[:40] if len(v2) == 64 else ""}
    return wanted in identities


def magnet_matches_saved_hash(magnet_url: str, saved_hash: str, client: TorrentClientAdapter | None) -> bool:
    identities = parse_magnet_hashes(magnet_url)
    old = str(saved_hash or "").upper()
    if not identities or not old:
        return False
    btih, btmh = identities
    if old in btmh or any(old == value[:40] for value in btmh):
        return True
    if old not in btih:
        return False
    if not btmh:
        return True
    info = client.inspect_torrent(old) if client is not None else None
    if not isinstance(info, dict) or not _client_info_matches_hash(info, old):
        return False
    known_v2 = str(info.get("infohash_v2") or "").upper()
    return known_v2 in btmh


def info_owned_by_tow(info: dict[str, Any] | None, infohash: str) -> bool:
    """The client's report (``inspect_torrent``) is of this torrent and carries TOW's mark."""
    if not info or not _client_info_matches_hash(info, infohash):
        return False
    tags = info.get("tags")
    if not isinstance(tags, (list, tuple, set)):
        return False
    return any(str(tag).strip().casefold() == "tow" for tag in tags)


def info_confirms(
    info: dict[str, Any] | None, infohash: str, save_path: str, *, require_tow_ownership: bool = False
) -> bool:
    """The client's report confirms the torrent in ``save_path`` (and with TOW's mark when asked)."""
    if require_tow_ownership and not info_owned_by_tow(info, infohash):
        return False
    if not info or not _client_info_matches_hash(info, infohash):
        return False
    observed_path = str(info.get("save_path") or "").strip()
    return bool(observed_path) and paths_equal(observed_path, save_path)


def client_owned_by_tow(client: TorrentClientAdapter, infohash: str) -> bool:
    return info_owned_by_tow(client.inspect_torrent(infohash), infohash)


def confirm_client_add(
    client: TorrentClientAdapter, infohash: str, save_path: str, *, require_tow_ownership: bool = False
) -> bool:
    """Read-back of an add or a change, from one read of the torrent: a torrent the client does
    not have reports None, so asking has_hash first was a second read (a full one, with the file
    list, in a ManagedClient), and the ownership check two more."""
    return info_confirms(
        client.inspect_torrent(infohash), infohash, save_path, require_tow_ownership=require_tow_ownership
    )


RELOCATION_WAIT_SEC = 20.0


def await_relocation(
    client: TorrentClientAdapter, infohash: str, save_path: str, *, timeout: float | None = None
) -> str:
    """Wait for a client-side move after ``set_location``.

    qBittorrent reports the new ``save_path`` only once the data move finishes
    (instant on the same volume, minutes across disks). Returns ``"done"`` when
    the read-back confirms the path, ``"moving"`` when the client is still moving
    the data at the deadline, and ``"failed"`` otherwise.
    """
    deadline = time.monotonic() + max(0.0, RELOCATION_WAIT_SEC if timeout is None else timeout)
    while True:
        info = client.inspect_torrent(infohash)
        if info_confirms(info, infohash, save_path, require_tow_ownership=True):
            return "done"
        moving = str((info or {}).get("state") or "").casefold() == "moving"
        if time.monotonic() >= deadline:
            return "moving" if moving else "failed"
        time.sleep(0.5)


def _active_revision_overlap(client: TorrentClientAdapter, old_hash: str, new_files: tuple[Any, ...]) -> str:
    if not old_hash:
        return ""
    info = client.inspect_torrent(old_hash)  # None when the client does not have it: one read
    if not info or not _client_info_matches_hash(info, old_hash):
        return ""
    state = str(info.get("state") or "").casefold()
    if not state or state.startswith(("stopped", "paused")):
        return ""
    wanted = [windows_path_key(str(file.path)) for file in new_files if not getattr(file, "is_pad", False)]
    for row in info.get("files") or []:
        try:
            if row.get("priority") is not None and int(row.get("priority")) == 0:
                continue
        except TypeError, ValueError:
            continue
        raw_actual = str(row.get("name") or "")
        actual = windows_path_key(raw_actual)
        # A client that could not decode a legacy (non-UTF-8) name shows it as `_`/`?`/U+FFFD;
        # such a name cannot be compared, so fail closed. Readable names compare exactly.
        actual_lost_text = "�" in raw_actual or not any(ord(ch) > 127 for ch in raw_actual)
        for path in wanted:
            if actual == path or actual.endswith("/" + path):
                return raw_actual or path
            if (
                actual_lost_text
                and any(ord(character) > 127 for character in path)
                and len(actual.split("/")) in {len(path.split("/")), len(path.split("/")) + 1}
            ):
                return raw_actual or path
    return ""


def _conflicting_topic_claim(
    state: dict[str, Any],
    topic: Topic,
    infohash: str,
    save_path: str,
    selected_files: tuple[str, ...],
    selection_mode: str,
) -> str:
    """Detect incompatible topic records that would mutate the same client task."""
    wanted_files = {value.replace("\\", "/").casefold() for value in selected_files}
    client_id = str(topic.get("client_id") or "default")
    for other in topics_of(state):
        if other is topic or str(other.get("id") or "") == str(topic.get("id") or ""):
            continue
        if str(other.get("client_id") or client_id) != client_id:
            continue
        if str(other.get("hash") or "").upper() != infohash.upper():
            continue
        other_path = str(other.get("save_path") or "")
        other_files = {str(value).replace("\\", "/").casefold() for value in other.get("selected_files") or []}
        other_selection = as_dict(other.get("selection"))
        other_mode = str(other_selection.get("mode") or "all")
        compatible_selection = selection_mode == "all" and other_mode == "all"
        if not compatible_selection:
            compatible_selection = (
                bool(other_files) and not bool(other.get("selected_files_truncated")) and other_files == wanted_files
            )
        if not paths_equal(other_path, save_path) or not compatible_selection:
            # The topic by the name Home shows (its internal id said nothing: "another topic (2821ad16c798)").
            name = shown_title(other).split(" / ")[0].strip()
            return name or str(other.get("id") or "unknown")
    return ""


def _client_title(cfg: dict[str, Any], client_id: str) -> str:
    try:
        row = client_factory.client_configuration(cfg, client_id)
    except RuntimeError, ValueError:
        return client_id
    return str(row.get("title") or row.get("kind") or client_id)


# The code of "the previous revision still runs in the client" (Home offers to stop it).
PREVIOUS_REVISION_ACTIVE = "check.previous_revision_active"


_LEGACY_PREVIOUS_REVISION = "previous torrent revision is still active"


def blocked_by_previous_revision(topic: Topic) -> bool:
    """The topic waits for its previous revision to be stopped in the client (G1)."""
    if topic.get("last_error_code"):
        return topic.get("last_error_code") == PREVIOUS_REVISION_ACTIVE
    return str(topic.get("last_error") or "").startswith(_LEGACY_PREVIOUS_REVISION)


def blocked_revision(topic: Topic) -> str:
    """The hash of the revision the topic waits to add while its previous one still runs ("":
    it does not wait, or an older TOW did not record which revision it was)."""
    if topic.get("last_error_code") != PREVIOUS_REVISION_ACTIVE:
        return ""
    params = topic.get("last_error_params")
    return str((params if isinstance(params, dict) else {}).get("hash") or "").upper()


def client_unreachable(run: Any, client_id: str) -> TowError:
    """The topic's client did not answer this run: its own error (or "no connection") as the reason."""
    cause = run.client_errors.get(client_id)
    if isinstance(cause, TowError):
        reason: Any = cause
    elif cause is not None:
        reason = str(cause) or type(cause).__name__
    else:
        reason = Msg("check.no_connection")
    return TowError("check.client_unreachable", reason=reason)


def live_previous_overlap(client: TorrentClientAdapter, revision_hashes: list[str], files: Any) -> str:
    """A file of ``files`` that an earlier revision still running in the client also downloads
    ("" when none does): the new revision must not start beside it (G1)."""
    for revision_hash in dict.fromkeys(revision_hashes):
        overlap = _active_revision_overlap(client, revision_hash, tuple(files))
        if overlap:
            return overlap
    return ""


def assert_client_can_add(
    state: dict[str, Any],
    topic: Topic,
    topic_client: TorrentClientAdapter,
    *,
    h: str,
    old: str,
    dest: str,
    plan: Any,
    files: tuple[Any, ...],
    replaces_revision: bool,
) -> None:
    """Refuse a client mutation that could clash with another topic or a live revision."""
    conflicting_topic = _conflicting_topic_claim(state, topic, h, dest, plan.selected_files, plan.mode)
    if conflicting_topic:
        raise TowError("check.hash_claimed", topic=conflicting_topic)
    if replaces_revision:
        overlap = live_previous_overlap(topic_client, [old, *map(str, topic.get("previous_hashes") or [])], files)
        if overlap:
            raise TowError(PREVIOUS_REVISION_ACTIVE, file=overlap, hash=h.upper())
    capabilities = topic_client.capabilities or {}
    if not all(
        capabilities.get(name) is True for name in ("stopped_add", "file_selection", "priority_readback", "start_stop")
    ):
        raise TowError("check.client_cannot_select")


def info_is_pending_tow_add(existing_info: dict[str, Any] | None, h: str) -> bool:
    """The client's report says the torrent is still marked tow-pending: an earlier run added it
    but did not finish."""
    existing_tags = {str(tag).strip().casefold() for tag in (existing_info or {}).get("tags") or []}
    return bool(
        existing_info and _client_info_matches_hash(existing_info, h) and {"tow", "tow-pending"}.issubset(existing_tags)
    )


def remote_clients(cfg: dict[str, Any], secrets: dict[str, Any]) -> frozenset[str]:
    """The configured clients that run on another computer."""
    try:
        rows = client_factory.client_configurations(cfg)
    except TowError, RuntimeError, ValueError:
        return frozenset()
    return frozenset(
        str(row["id"]) for row in rows if not client_factory.on_this_computer(cfg, secrets, str(row["id"]))
    )


def _open_client(cfg: dict[str, Any], secrets: dict[str, Any], client_id: str, *, apply: bool) -> TorrentClientAdapter:
    """The client adapter; for a dry run in its read-only mode where it has one (Deluge does
    not attach its Web UI to a daemon during a preview)."""
    adapter = client_factory.from_secrets(cfg, secrets, client_id)
    if not apply:
        adapter.read_only = True
    return adapter


# How many checks in a row a client that lists nothing is taken for one still starting.
EMPTY_CLIENT_CHECKS = 2


@dataclass
class ClientPool:
    """The torrent clients one run talks to: opened on first use, a client that does not answer
    is not asked again in this run (and reported once, like its recovery)."""

    cfg: dict[str, Any]
    apply: bool
    notify: bool
    how: str
    record: Callable[..., None]
    batch: NotificationBatch
    default_id: str
    previous_qbit_ok: Any
    previous_clients_ok: dict[str, Any]
    clients: dict[str, TorrentClientAdapter] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    ok: dict[str, bool] = field(default_factory=dict)
    ping: str = ""
    # Clients that had TOW's torrents at the last check (a topic this run checks was fine then).
    expect_torrents: frozenset[str] = frozenset()
    # How many checks in a row each client listed nothing (before this run, and with it).
    previous_empty: dict[str, int] = field(default_factory=dict)
    empty: dict[str, int] = field(default_factory=dict)
    # The clients this run pinged: only their count is this run's, the others keep theirs.
    pinged: set[str] = field(default_factory=set)
    # False when the run had no topic to check and asked no client: its health is not this run's.
    asked: bool = True

    def _answered(self, client_id: str, client: TorrentClientAdapter) -> str:
        """Ping the client; one that lists no torrent at all, although it had TOW's at the last
        check, is still loading them after a start (qBittorrent answers meanwhile): this run
        must not read its torrents as removed and add them again. A start takes a check or
        two (a refusal marks the topics with an error, so the client refused last time is still
        expected to list them); a client that still lists nothing after that was emptied, and
        is believed."""
        self.pinged.add(client_id)
        answer = client.ping()
        has_any = getattr(client, "has_any_torrent", None)
        before = int(self.previous_empty.get(client_id) or 0)
        expected = client_id in self.expect_torrents or 0 < before < EMPTY_CLIENT_CHECKS
        if expected and callable(has_any) and has_any() is False:
            runs = before + 1
            self.empty[client_id] = runs
            if runs <= EMPTY_CLIENT_CHECKS:
                raise TowError("check.client_empty", cls="qbit")
        return answer

    def _client_event(self, client_id: str, kind: str, title: str = "") -> None:
        self.batch.queue(
            {"id": f"__client__:{client_id}", "title": title}, kind=kind, operation_id=new_operation_id("bot")
        )

    def open_default(self, secrets: dict[str, Any]) -> None:
        """Ping the main client; only a client that answered is used (a dead one made every
        topic fail, and notify)."""
        client_id = self.default_id
        try:
            client = _open_client(self.cfg, secrets, client_id, apply=self.apply)
            self.ping = self._answered(client_id, client)
            self.clients[client_id] = client
        except Exception as e:  # noqa: BLE001 - any failure to reach the client is 'the client is down' (recorded)
            self.ping = f"down: {e}"
            self.errors[client_id] = e
            self.record(
                "client_unreachable",
                component="client",
                integration_id=client_id,
                client_kind="unknown",
                **error_fields(e),
                how=self.how,
            )
            if self.notify and self.previous_qbit_ok is not False:
                self._client_event(client_id, "qbit_down")
        else:
            if self.notify and self.previous_qbit_ok is False:
                self._client_event(client_id, "qbit_up")

    def get(self, topic: Topic, secrets: dict[str, Any]) -> tuple[str, TorrentClientAdapter | None]:
        """The topic's client (None when it does not answer this run)."""
        wanted = str(topic.get("client_id") or self.default_id)
        if wanted in self.clients:
            return wanted, self.clients[wanted]
        if wanted in self.errors:
            return wanted, None
        try:
            adapter = _open_client(self.cfg, secrets, wanted, apply=self.apply)
            self._answered(wanted, adapter)  # a second client that is down is reported like the main one
        except Exception as e:  # noqa: BLE001 - any failure to reach the client is 'the client is down' (recorded)
            self.errors[wanted] = e
            self.ok[wanted] = False
            self.record(
                "client_unreachable", component="client", integration_id=wanted, **error_fields(e), how=self.how
            )
            if self.notify and self.previous_clients_ok.get(wanted) is not False:
                self._client_event(wanted, "qbit_down", _client_title(self.cfg, wanted))
            return wanted, None
        self.clients[wanted] = adapter
        self.ok[wanted] = True
        return wanted, adapter

    def queue_recovered(self) -> None:
        """A second client that was down in the previous run answered again."""
        for client_id, ok in self.ok.items():
            if client_id != self.default_id and ok and self.previous_clients_ok.get(client_id) is False and self.notify:
                self._client_event(client_id, "qbit_up", _client_title(self.cfg, client_id))

    @property
    def default_ok(self) -> bool:
        return not str(self.ping).startswith("down")

    def empty_counts(self) -> dict[str, int]:
        """Every client's 'listed nothing' count after this run: a client this run did not ask
        keeps its earlier one (a check of another client's topic must not restart its count)."""
        kept = {k: v for k, v in self.previous_empty.items() if k not in self.pinged and v > 0}
        return {**kept, **self.empty}

    def health(self) -> dict[str, bool]:
        """Every client's state for the header: the earlier ones, then what this run saw."""
        return {**self.previous_clients_ok, self.default_id: self.default_ok, **self.ok}
