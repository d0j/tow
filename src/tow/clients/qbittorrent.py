from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import quote

from qbittorrentapi import Client

from tow.clients import files
from tow.clients.managed import OWNER, PENDING, ClientError, ManagedClient
from tow.clients.spec import TorrentClientAdapter
from tow.errors import Msg
from tow.torrent import TorrentFile, parse_magnet_hashes, parse_torrent_metadata

_LOG = logging.getLogger("tow.clients")

KIND = "qbittorrent"
TITLE = "qBittorrent"  # one name everywhere (Settings, the header, the add form)
SECRETS_KEY = "qbittorrent"
READY = True
DEFAULT_PORT = 8080
ORDER = 10
SHORT = "qBit"
# Texts are keys of the language files (client.qbittorrent.*).
STEPS = (
    "client.qbittorrent.step_open",
    "client.qbittorrent.step_enable",
    "client.qbittorrent.step_address",
    "client.qbittorrent.step_save",
)
NOTE = "client.qbittorrent.note"
# Who reports the errors (in front of each message).
NAME = "qBittorrent"
# Seconds an add may take (reading a large .torrent); other requests keep the 8 s timeout.
ADD_TIMEOUT_SEC = 60
# A check's observations (reconcile asks about every topic's torrent) read one listing of all
# torrents for this long, not one request per torrent; any change TOW makes drops it, and a
# read-back never uses it (inspect_torrent always asks the client).
LISTING_SEC = 60.0
# The Web API version, which qbittorrent-api asks before every add, start and stop.
WEB_API_VERSION_SEC = 600.0


def _fail(code: str, /, **params: Any) -> ClientError:
    """A qBittorrent failure: ``client.qbittorrent.*`` or the shared ``client.managed.*`` text."""
    return ClientError(code, prefix=NAME, **params)


class QBittorrentClient(ManagedClient):
    """qBittorrent on the shared contract of ``tow.clients.managed``. Its own: the add (tags for
    TOW's marks and the category apart, no automatic torrent management, a long timeout, a
    repeated add accepted when it is TOW's), lookup by any of a hybrid torrent's hashes,
    selecting every file by count, priority levels kept by a rollback, and magnet metadata."""

    title = TITLE
    kind = KIND
    client_kind = KIND
    capabilities: ClassVar[dict[str, bool]] = {
        **ManagedClient.capabilities,
        "magnet_metadata": True,
        "metadata_preview": True,
    }

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._c = Client(
            host=host,
            port=port,
            username=username,
            password=password,
            REQUESTS_ARGS={"timeout": 8},
        )
        self._listing: tuple[float, dict[str, list[Any]]] | None = None
        self._web_api: tuple[float, str] | None = None
        if callable(getattr(type(self._c), "app_web_api_version", None)):  # the library's client
            asked = self._c.app_web_api_version
            self._c.app_web_api_version = self._cached_web_api_version(asked)  # type: ignore[method-assign]

    def _cached_web_api_version(self, asked: Any) -> Any:
        """qbittorrent-api asks the Web API version before every add, start and stop (30 times
        in a check of 200 topics): one answer serves the adapter for WEB_API_VERSION_SEC."""

        def web_api_version(**kwargs: Any) -> str:
            if kwargs:
                return str(asked(**kwargs))
            now = time.monotonic()
            if self._web_api is None or now - self._web_api[0] > WEB_API_VERSION_SEC:
                self._web_api = (now, str(asked()))
            return self._web_api[1]

        return web_api_version

    def _changing(self) -> None:
        """TOW is about to change the client: the listing no longer says how it is."""
        self._listing = None

    def _listed(self, infohash: str) -> list[Any]:
        """The rows of one listing of every torrent under the id ``infohash`` (what asking
        ``torrents/info?hashes=`` for it returns)."""
        now = time.monotonic()
        if self._listing is None or now - self._listing[0] > LISTING_SEC:
            index: dict[str, list[Any]] = {}
            for row in self._c.torrents_info() or []:
                index.setdefault(str(getattr(row, "hash", "") or "").upper(), []).append(row)
            self._listing = (now, index)
        return self._listing[1].get(str(infohash or "").upper(), [])

    def observe_torrent(self, infohash: str) -> dict[str, Any] | None:
        """``inspect_torrent`` for an observation (reconcile): the torrent's row from the
        run's listing, its files asked. A torrent the listing does not show (or shows twice)
        is asked on its own before it counts as gone. Never for a read-back."""
        rows = self._listed(infohash)
        if len(rows) != 1:
            return self.inspect_torrent(infohash)
        return self._report(rows[0], self._c.torrents_files(torrent_hash=infohash.lower()), infohash)

    def ping(self) -> str:
        ver = str(self._c.app.version)
        web = str(self._c.app.web_api_version)
        return f"{ver} webapi {web}"

    def has_any_torrent(self) -> bool:
        """The client lists at least one torrent. qBittorrent answers while it still loads its
        torrents after a start, with fewer (or none) of them."""
        return bool(self._c.torrents_info(limit=1))

    def has_hash(self, infohash: str) -> bool:
        rows = self._c.torrents_info(torrent_hashes=infohash.lower())
        return bool(rows)

    # --- the shared contract's primitives ------------------------------------------------------

    def _owner_tags(self, infohash: str) -> list[str] | None:
        """One torrent row, no file list."""
        rows = self._c.torrents_info(torrent_hashes=infohash.lower())
        return self._tag_list(rows[0]) if rows else None

    def _priority(self, infohash: str, file_ids: list[int], priority: int) -> None:
        self._changing()
        self._c.torrents_file_priority(torrent_hash=infohash.lower(), file_ids=file_ids, priority=priority)

    def _set_wanted(self, infohash: str, wanted: set[int], all_ids: list[int]) -> None:
        self._priority(infohash, all_ids, 0)
        self._require_owned(infohash)
        self._priority(infohash, sorted(wanted), 1)

    def _stop(self, infohash: str) -> None:
        self._changing()
        method = getattr(self._c, "torrents_stop", None) or getattr(self._c, "torrents_pause", None)
        if not callable(method):
            raise _fail("client.qbittorrent.no_stop")
        method(torrent_hashes=infohash.lower())

    def _start(self, infohash: str) -> None:
        self._changing()
        method = getattr(self._c, "torrents_start", None) or getattr(self._c, "torrents_resume", None)
        if not callable(method):
            raise _fail("client.qbittorrent.no_start")
        method(torrent_hashes=infohash.lower())

    def _add_label(self, infohash: str, tags: list[str], label: str) -> None:
        """A tag, never the category: with automatic torrent management a category moves the files."""
        self._changing()
        self._c.torrents_add_tags(tags=label, torrent_hashes=infohash.lower())

    def _remove_label(self, infohash: str, tags: list[str], label: str) -> None:
        self._changing()
        remove = getattr(self._c, "torrents_remove_tags", None)
        if not callable(remove):
            raise _fail("client.qbittorrent.no_tag_removal")
        remove(tags=label, torrent_hashes=infohash.lower())

    def _move(self, infohash: str, save_path: str) -> None:
        self._changing()
        self._c.torrents_set_location(location=save_path, torrent_hashes=infohash.lower())

    def _resolved_hash(self, infohash: str, *, allow_full_scan: bool = True) -> str | None:
        wanted = str(infohash or "").upper()
        rows = self._c.torrents_info(torrent_hashes=wanted.lower())
        if not rows and allow_full_scan:
            try:
                rows = self._c.torrents_info()
            except TypeError:
                return None
        matches: set[str] = set()
        for row in rows or []:
            primary = str(getattr(row, "hash", "") or "").upper()
            v1 = str(getattr(row, "infohash_v1", "") or "").upper()
            v2 = str(getattr(row, "infohash_v2", "") or "").upper()
            identities = {primary, v1, v2, v2[:40] if len(v2) == 64 else ""}
            if wanted in identities and primary:
                matches.add(primary)
        if len(matches) > 1:
            raise _fail("client.qbittorrent.hash_ambiguous")
        return next(iter(matches), None)

    def _web_api_at_least(self, minimum: tuple[int, ...]) -> bool:
        raw = str(self._c.app.web_api_version or "")
        try:
            current = tuple(int(part) for part in raw.split("."))
        except ValueError:
            return False
        width = max(len(current), len(minimum))
        return current + (0,) * (width - len(current)) >= minimum + (0,) * (width - len(minimum))

    def materialize_magnet(
        self,
        magnet_url: str,
        save_path: str | None,
        infohash: str,
    ) -> bytes:
        """Fetch magnet metadata without downloading media payload bytes."""
        destination = str(save_path or "").strip()
        expected = str(infohash or "").strip().upper()
        btih, btmh = self._magnet_identity(magnet_url, destination, expected)
        self._changing()
        lookup_hash = expected[:40] if len(expected) == 64 else expected
        resolved = self._resolved_hash(lookup_hash)
        if resolved is not None:
            resolved = self._drop_stale_magnet(resolved, lookup_hash)
        created = resolved is None
        try:
            if created:
                result = self._c.torrents_add(
                    urls=magnet_url,
                    save_path=destination,
                    use_auto_torrent_management=False,
                    tags="tow,tow-pending",
                    content_layout="Original",
                    stop_condition="MetadataReceived",
                )
                self._api_ok(result, action="client.qbittorrent.action_add_magnet")
                resolved, _owned = self._wait_for_ownership(
                    lookup_hash,
                    visibility_error="client.managed.not_visible",
                    ownership_error="client.managed.no_owner_mark",
                    aliases=tuple(sorted(btih | btmh)),
                )
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                current = self._resolved_hash(lookup_hash)
                if current:
                    resolved = current
                target = resolved or lookup_hash
                inspected = self.inspect_torrent(target)
                if inspected is not None and inspected.get("files"):
                    if created:
                        self._stop_before_export(target)
                    content = self._export_magnet(target, btih, btmh)
                    if content is not None:
                        return content
                time.sleep(0.25)
        except Exception:
            if created:
                self._stop_pending_magnet(resolved, (lookup_hash, *sorted(btih | btmh)))
            raise
        if created and resolved:
            self._stop(resolved)
        raise _fail("client.qbittorrent.magnet_timeout")

    def preview_magnet(self, magnet_url: str) -> bytes:
        """Explicit native metadata retrieval; never add, stop, tag or delete a transfer.

        The Web API has no metadata-only cancel endpoint. On timeout the client's native
        peer request may continue (upload mode), so never use a normal torrent delete as
        cleanup: another user may have added that hash meanwhile.
        """
        hashes = parse_magnet_hashes(magnet_url)
        if hashes is None:
            raise _fail("client.qbittorrent.magnet_invalid")
        btih, btmh = hashes
        try:
            # Export an existing task read-only, even on clients predating fetchMetadata.
            for identity in sorted(btih | btmh):
                target = self._resolved_hash(identity)
                if target is not None:
                    exported = self._export_magnet(target, btih, btmh)
                    if exported is not None:
                        return exported
            if self.read_only or not self._web_api_at_least((2, 11, 9)):
                raise _fail("content.magnet_unsupported")
            # qBittorrent decodes source once more after parsing the POST form. Preserve
            # escaped tracker query delimiters/passkeys instead of turning them into
            # magnet parameters. Use the identical source for fetch and save.
            source = quote(magnet_url, safe="")
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                result = self._c.torrents_fetch_metadata(source=source)
                # Async responses may contain just info hashes rather than being empty.
                if isinstance(result, Mapping) and result.get("info"):
                    # A task another user added during prefetch is not necessarily in
                    # qBittorrent's metadata cache. Export it without touching its state.
                    for identity in sorted(btih | btmh):
                        target = self._resolved_hash(identity)
                        if target is not None:
                            exported = self._export_magnet(target, btih, btmh)
                            if exported is not None:
                                return exported
                    content = self._c.torrents_save_metadata(source=source)
                    metadata = parse_torrent_metadata(content)
                    if (btih and metadata.hash_v1 not in btih) or (btmh and metadata.hash_v2 not in btmh):
                        raise _fail("client.qbittorrent.magnet_data_mismatch")
                    return content
                time.sleep(0.25)
        except ClientError:
            raise
        except Exception as exc:
            raise _fail("content.magnet_failed") from exc
        raise _fail("content.magnet_timeout")

    def _magnet_identity(
        self, magnet_url: str, destination: str, expected: str
    ) -> tuple[frozenset[str], frozenset[str]]:
        """The magnet's (BTIH, BTMH) hashes, once it is usable here for ``expected``."""
        if not destination:
            raise _fail("client.managed.no_folder")
        hashes = parse_magnet_hashes(str(magnet_url or ""))
        if hashes is None:
            raise _fail("client.qbittorrent.magnet_invalid")
        btih, btmh = hashes
        if expected not in btih | btmh:
            raise _fail("client.qbittorrent.magnet_hash_mismatch")
        if not self._web_api_at_least((2, 8, 15)):
            raise _fail("client.qbittorrent.webapi_too_old")
        return btih, btmh

    def _drop_stale_magnet(self, resolved: str, lookup_hash: str) -> str | None:
        """A stopped magnet TOW left pending without metadata is removed (and None returned) so
        it can be added again; any other torrent under the hash is kept (its hash returned)."""
        existing = self.inspect_torrent(resolved)
        if (existing or {}).get("files") or PENDING not in self._tags(existing):
            return resolved
        if not self._stopped(existing):
            raise _fail("client.qbittorrent.pending_magnet_active")
        delete = getattr(self._c, "torrents_delete", None)
        if not callable(delete):
            raise _fail("client.qbittorrent.cannot_recreate_magnet")
        self._changing()
        self._api_ok(
            delete(delete_files=False, torrent_hashes=resolved.lower()),
            action="client.qbittorrent.action_remove_magnet",
        )
        for _attempt in range(20):
            if self._resolved_hash(lookup_hash) is None:
                return None
            time.sleep(0.1)
        raise _fail("client.qbittorrent.stale_magnet_not_removed")

    def _stop_before_export(self, infohash: str) -> None:
        """The magnet TOW added has its metadata: stopped, and proven to have fetched no payload."""
        self._stop(infohash)
        stopped = self._wait_stopped(infohash)
        if int((stopped or {}).get("downloaded") or 0) > 0:
            raise _fail("client.qbittorrent.payload_downloaded")

    def _export_magnet(self, infohash: str, btih: frozenset[str], btmh: frozenset[str]) -> bytes | None:
        """The .torrent the client built from the magnet (None: not exportable yet), checked
        against the magnet's hashes."""
        try:
            content = self._c.torrents_export(torrent_hash=infohash.lower())
        except Exception as exc:  # noqa: BLE001 - the client may not have the file written yet: asked again
            _LOG.info("magnet export not ready: %s", type(exc).__name__)
            return None
        metadata = parse_torrent_metadata(bytes(content))
        if btih and metadata.hash_v1 not in btih:
            raise _fail("client.qbittorrent.magnet_data_mismatch")
        if btmh and metadata.hash_v2 not in btmh:
            raise _fail("client.qbittorrent.magnet_data_mismatch")
        return bytes(content)

    def _stop_pending_magnet(self, resolved: str | None, candidates: tuple[str, ...]) -> None:
        """A failed magnet fetch stops the torrent it added (only one still marked tow-pending)."""
        cleanup_hash = resolved
        if cleanup_hash is None:
            for candidate in candidates:
                cleanup_hash = self._resolved_hash(candidate)
                if cleanup_hash:
                    break
        inspected = self.inspect_torrent(cleanup_hash) if cleanup_hash else None
        if cleanup_hash and {OWNER, PENDING}.issubset(self._tags(inspected)):
            self._stop(cleanup_hash)

    @staticmethod
    def _api_ok(result: Any, *, action: str) -> None:
        """``action``: the catalog key naming what was asked (it opens the error message)."""
        if isinstance(result, str) and result.strip().lower() not in {"ok", "ok."}:
            raise _fail("client.qbittorrent.api_refused", action=Msg(action), answer=result.strip()[:200])
        if isinstance(result, Mapping):
            try:
                succeeded = int(result.get("success_count") or 0)
                pending = int(result.get("pending_count") or 0)
                failed = int(result.get("failure_count") or 0)
            except (TypeError, ValueError) as exc:
                raise _fail("client.qbittorrent.api_malformed", action=Msg(action)) from exc
            if failed or succeeded + pending != 1:
                raise _fail(
                    "client.qbittorrent.api_counts",
                    action=Msg(action),
                    succeeded=succeeded,
                    pending=pending,
                    failed=failed,
                )

    def _added_by_this_add(self, hashes: tuple[str, ...]) -> bool:
        """After a failed add request: the torrent is there with both of TOW's marks (it was
        not there before, so this add put it there)."""
        for value in dict.fromkeys(str(h).strip().upper() for h in hashes if str(h or "").strip()):
            try:
                info = self.inspect_torrent(value)
            except Exception:  # noqa: BLE001 - not readable now: the add's own error stands
                return False
            if {OWNER, PENDING}.issubset(self._tags(info)):
                return True
        return False

    def _wait_for_ownership(
        self,
        infohash: str,
        *,
        visibility_error: str,
        ownership_error: str,
        aliases: tuple[str, ...] = (),
        attempts: int = 50,
    ) -> tuple[str, dict[str, Any]]:
        """Wait until an accepted add is queryable with both transactional tags (the errors are
        catalog keys). Returns the hash qBittorrent lists it under, found by any of its hashes."""
        observed: dict[str, Any] | None = None
        candidates = tuple(
            dict.fromkeys(str(value).strip().upper() for value in (infohash, *aliases) if str(value or "").strip())
        )

        def owned(candidate: str, *, full_scan: bool) -> str | None:
            nonlocal observed
            resolved = self._resolved_hash(candidate, allow_full_scan=full_scan)
            if resolved is None:
                return None
            observed = self.inspect_torrent(resolved)
            self._check_state(observed)
            return resolved if {OWNER, PENDING}.issubset(self._tags(observed)) else None

        for attempt in range(attempts):
            for candidate in candidates:
                if (resolved := owned(candidate, full_scan=False)) is not None:
                    return resolved, observed or {}
            if attempt % 10 == 9 and (resolved := owned(candidates[0], full_scan=True)) is not None:
                return resolved, observed or {}
            if attempt + 1 < attempts:
                self._sleep()
        if observed is None:
            raise _fail(visibility_error)
        raise _fail(ownership_error)

    def _inspect_with_files(self, infohash: str) -> dict[str, Any]:
        """Up to 20 reads. Unlike the shared wait, a torrent in an error state is returned too:
        the step after it reports that state."""
        for _attempt in range(20):
            inspected = self.inspect_torrent(infohash)
            if inspected is not None and inspected.get("files"):
                return inspected
            self._sleep()
        raise _fail("client.managed.no_files")

    def _apply_selection(
        self, infohash: str, source_files: tuple[TorrentFile, ...], selected: set[int], root_name: str
    ) -> dict[str, Any]:
        """Every file of a torrent without padding is selected by count, not by name: qBittorrent
        may show a name the torrent spells in another encoding. Anything else is the shared,
        name-and-size matched selection."""
        every = {row.index for row in source_files if not row.is_pad}
        if selected != every or any(row.is_pad or self._padding_like(row.path) for row in source_files):
            return super()._apply_selection(infohash, source_files, selected, root_name)

        def fail() -> ClientError:
            return _fail("client.managed.wrong_selection")

        inspected = self._inspect_with_files(infohash)
        rows = list(inspected.get("files") or [])
        files.priorities(rows, fail=fail)
        ids = sorted(
            int(row["index"])
            for row in rows
            if isinstance(row.get("index"), int) and not self._padding_like(row.get("name"))
        )
        if len(ids) != len(every):
            raise fail()
        self._require_owned(infohash)
        self._priority(infohash, ids, 1)
        verified = self._inspect_with_files(infohash)
        files.verify_selection(
            rows,
            list(verified.get("files") or []),
            set(ids),
            fail=fail,
            ignored={row["index"] for row in rows if self._padding_like(row.get("name"))},
        )
        self._require_owned(infohash)
        return verified

    def _ignored_padding(
        self,
        rows: list[dict[str, Any]],
        mapped_ids: set[int],
        source_files: tuple[TorrentFile, ...],
        selected: set[int],
    ) -> set[int]:
        """Only when every file is wanted: a partial selection first sets every file qBittorrent
        lists to skipped, so even a padding file must read back so."""
        if selected != {row.index for row in source_files if not row.is_pad}:
            return set()
        return super()._ignored_padding(rows, mapped_ids, source_files, selected)

    def _restore_selection(self, infohash: str, previous: dict[int, int], before_rows: list[dict[str, Any]]) -> None:
        """qBittorrent's priority levels (1 normal, 6 high, 7 maximal) come back as they were, one
        request per level, and are read back exactly."""

        def fail() -> ClientError:
            return _fail("client.managed.wrong_selection")

        for priority in sorted(set(previous.values())):
            self._require_owned(infohash)
            self._priority(infohash, sorted(index for index, value in previous.items() if value == priority), priority)
        restored = list(self._inspect_with_files(infohash).get("files") or [])
        restored_priorities = files.priorities(restored, fail=fail)
        files.verify_selection(before_rows, restored, {i for i, p in previous.items() if p > 0}, fail=fail)
        if restored_priorities != previous:
            raise fail()

    def _stop_after_add(self, added: dict[str, Any]) -> bool:
        """qBittorrent is asked to stop every torrent TOW adds, even one it lists stopped."""
        return True

    def add_torrent_selected(
        self,
        content: bytes,
        save_path: str | None,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
        *,
        start: bool = True,
    ) -> dict[str, Any]:
        metadata, destination, selected = self._check_add(content, save_path, infohash, selected_indices)
        self._changing()
        # G8: the owner's optional category and extra tags; "tow"/"tow-pending" stay first.
        extra_tags = [tag for tag in self.add_tags if tag not in {OWNER, PENDING}]
        category = str(self.add_category or "").strip()
        if category:
            with contextlib.suppress(Exception):  # it may exist already; the add reports real problems
                self._c.torrents_create_category(name=category)
        add_options: dict[str, Any] = {
            "torrent_files": content,
            "save_path": destination,
            "use_auto_torrent_management": False,
            "tags": ",".join([OWNER, PENDING, *extra_tags]),
            **({"category": category} if category else {}),
            "is_stopped": True,
            "content_layout": "Original",
        }
        aliases = tuple(
            value
            for value in (metadata.hash_v1, metadata.hash_v2, metadata.hash_v2[:40] if metadata.hash_v2 else None)
            if value
        )
        try:
            # A large .torrent can take qBittorrent a while; its library sends a request that
            # timed out once more, and the add must not be one of them.
            result = self._c.torrents_add(**add_options, requests_args={"timeout": ADD_TIMEOUT_SEC})
            self._api_ok(result, action="client.qbittorrent.action_add")
        except Exception:
            # ...and when it was: qBittorrent refuses the repeat as a duplicate although the first
            # request added the torrent. It is this add when it is there now with both TOW marks.
            if not self._added_by_this_add((infohash, *aliases)):
                raise
        # Not stopped again when this fails: a torrent without both marks may not be TOW's.
        owned_hash, added = self._wait_for_ownership(
            infohash,
            visibility_error="client.managed.not_visible",
            ownership_error="client.managed.no_owner_mark",
            aliases=aliases,
        )
        return self._finish_add(owned_hash, added, destination, metadata, selected, start=start)

    def inspect_torrent(self, infohash: str) -> dict[str, Any] | None:
        rows = self._c.torrents_info(torrent_hashes=infohash.lower())
        if not rows:
            return None
        return self._report(rows[0], self._c.torrents_files(torrent_hash=infohash.lower()), infohash)

    def _report(self, torrent: Any, files: Any, infohash: str) -> dict[str, Any]:
        normalized_files = [
            {
                "index": getattr(row, "index", None),
                "name": str(getattr(row, "name", "") or ""),
                "size": getattr(row, "size", None),
                "progress": getattr(row, "progress", None),
                "priority": getattr(row, "priority", None),
            }
            for row in files or []
        ]
        return {
            "hash": str(getattr(torrent, "hash", infohash) or infohash),
            "infohash_v1": str(getattr(torrent, "infohash_v1", "") or ""),
            "infohash_v2": str(getattr(torrent, "infohash_v2", "") or ""),
            "progress": torrent.get("progress", 0.0)
            if isinstance(torrent, Mapping)
            else getattr(torrent, "progress", 0.0),
            "downloaded": getattr(torrent, "downloaded", 0),
            "added_on": getattr(torrent, "added_on", None),
            "completion_on": getattr(torrent, "completion_on", None),
            "save_path": getattr(torrent, "save_path", None),
            "content_path": getattr(torrent, "content_path", None),
            "state": str(getattr(torrent, "state", "") or ""),
            "tags": self._tag_list(torrent),
            "files": normalized_files,
        }

    @staticmethod
    def _tag_list(torrent: Any) -> list[str]:
        raw_tags = getattr(torrent, "tags", "")
        if isinstance(raw_tags, str):
            return sorted({part.strip() for part in raw_tags.split(",") if part.strip()})
        return sorted({str(part).strip() for part in (raw_tags or []) if str(part).strip()})


def from_secrets(secrets: dict[str, Any]) -> TorrentClientAdapter:
    q = secrets.get("qbittorrent") or {}
    if not q.get("host"):
        raise _fail("client.managed.no_address")
    return QBittorrentClient(
        host=str(q["host"]),
        port=int(q.get("port") or 8080),
        username=str(q.get("username") or ""),
        password=str(q.get("password") or ""),
    )
