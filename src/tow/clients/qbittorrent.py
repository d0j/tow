from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from qbittorrentapi import Client

from tow.clients import files
from tow.clients.managed import ClientError
from tow.clients.spec import TorrentClientAdapter
from tow.errors import Msg
from tow.folders import paths_equal
from tow.torrent import (
    TorrentFile,
    parse_magnet_hashes,
    parse_torrent_metadata,
)

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


def _fail(code: str, /, **params: Any) -> ClientError:
    """A qBittorrent failure: ``client.qbittorrent.*`` or the shared ``client.managed.*`` text."""
    return ClientError(code, prefix=NAME, **params)


class QBittorrentClient:
    kind = "qbittorrent"
    client_kind = "qbittorrent"
    client_id = "default"
    add_category = ""
    add_tags: Sequence[str] = ()
    read_only = False  # a preview only reads: nothing to switch off
    capabilities: ClassVar[dict[str, bool]] = {
        "inspect": True,
        "list_files": True,
        "completion_time": True,
        "add": True,
        "stopped_add": True,
        "file_selection": True,
        "priority_readback": True,
        "start_stop": True,
        "magnet_metadata": True,
    }

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._c = Client(
            host=host,
            port=port,
            username=username,
            password=password,
            REQUESTS_ARGS={"timeout": 8},
        )

    def ping(self) -> str:
        ver = str(self._c.app.version)
        web = str(self._c.app.web_api_version)
        return f"{ver} webapi {web}"

    def has_hash(self, infohash: str) -> bool:
        rows = self._c.torrents_info(torrent_hashes=infohash.lower())
        return bool(rows)

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
        tags = {str(tag).casefold() for tag in (existing or {}).get("tags") or []}
        state = str((existing or {}).get("state") or "").casefold()
        if (existing or {}).get("files") or "tow-pending" not in tags:
            return resolved
        if not state.startswith(("stopped", "paused")):
            raise _fail("client.qbittorrent.pending_magnet_active")
        delete = getattr(self._c, "torrents_delete", None)
        if not callable(delete):
            raise _fail("client.qbittorrent.cannot_recreate_magnet")
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
        tags = {str(tag).strip().casefold() for tag in (inspected or {}).get("tags") or []}
        if cleanup_hash and {"tow", "tow-pending"}.issubset(tags):
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
        catalog keys)."""
        observed: dict[str, Any] | None = None
        candidates = tuple(
            dict.fromkeys(str(value).strip().upper() for value in (infohash, *aliases) if str(value or "").strip())
        )
        for attempt in range(attempts):
            for candidate in candidates:
                resolved = self._resolved_hash(candidate, allow_full_scan=False)
                if resolved is not None:
                    observed = self.inspect_torrent(resolved)
                    tags = {str(tag).strip().casefold() for tag in (observed or {}).get("tags") or []}
                    if {"tow", "tow-pending"}.issubset(tags):
                        return resolved, observed or {}
            if attempt % 10 == 9:
                resolved = self._resolved_hash(candidates[0])
                if resolved is not None:
                    observed = self.inspect_torrent(resolved)
                    tags = {str(tag).strip().casefold() for tag in (observed or {}).get("tags") or []}
                    if {"tow", "tow-pending"}.issubset(tags):
                        return resolved, observed or {}
            if attempt + 1 < attempts:
                time.sleep(0.1)
        if observed is None:
            raise _fail(visibility_error)
        raise _fail(ownership_error)

    @staticmethod
    def _normalize_path(value: object) -> str:
        # The client may store `a: b` as `a_ b` on Windows; compare the sanitized spelling.
        return files.normalize_path(value)

    @classmethod
    def _padding_like_name(cls, value: object) -> bool:
        return files.padding_like(value)

    @classmethod
    def _map_files(
        cls,
        source_files: tuple[TorrentFile, ...],
        client_files: list[dict[str, Any]],
        root_name: str,
    ) -> dict[int, int]:
        return files.map_files(
            source_files,
            client_files,
            root_name,
            fail=lambda path: _fail("client.managed.file_unmatched", path=path),
        )

    def _inspect_with_files(self, infohash: str) -> dict[str, Any]:
        for _attempt in range(20):
            inspected = self.inspect_torrent(infohash)
            if inspected is not None and inspected.get("files"):
                return inspected
            time.sleep(0.1)
        raise _fail("client.managed.no_files")

    def _stop(self, infohash: str) -> None:
        method = getattr(self._c, "torrents_stop", None) or getattr(self._c, "torrents_pause", None)
        if not callable(method):
            raise _fail("client.qbittorrent.no_stop")
        method(torrent_hashes=infohash.lower())

    def _wait_stopped(self, infohash: str) -> dict[str, Any]:
        last: dict[str, Any] | None = None
        for attempt in range(50):
            last = self.inspect_torrent(infohash)
            if last is not None:
                state = str(last.get("state") or "").casefold()
                if state.startswith(("stopped", "paused")):
                    return last
                if state in {"error", "missingfiles", "unknown"}:
                    raise _fail("client.managed.error_state", state=state)
            if attempt < 49:
                time.sleep(0.1)
        raise _fail("client.managed.stop_unconfirmed")

    def stop_owned_torrent(self, infohash: str) -> dict[str, Any]:
        """G1: stop a torrent TOW added itself (tag "tow"), on the owner's explicit request."""
        info = self.inspect_torrent(infohash)
        if info is None:
            raise _fail("client.managed.missing")
        tags = {str(tag).strip().casefold() for tag in info.get("tags") or []}
        if "tow" not in tags:
            raise _fail("client.managed.not_owned_stop")
        if str(info.get("state") or "").casefold().startswith(("stopped", "paused")):
            return info
        self._stop(infohash)
        return self._wait_stopped(infohash)

    def _start(self, infohash: str) -> None:
        method = getattr(self._c, "torrents_start", None) or getattr(self._c, "torrents_resume", None)
        if not callable(method):
            raise _fail("client.qbittorrent.no_start")
        method(torrent_hashes=infohash.lower())

    def _wait_started(self, infohash: str) -> dict[str, Any]:
        last: dict[str, Any] | None = None
        for _attempt in range(50):
            last = self.inspect_torrent(infohash)
            if last is not None:
                state = str(last.get("state") or "").casefold()
                if state in {"error", "missingfiles", "unknown"}:
                    raise _fail("client.managed.error_state", state=state)
                if state and not state.startswith(("stopped", "paused")):
                    return last
                if state.startswith(("stopped", "paused")) and float(last.get("progress") or 0) >= 1:
                    return last
            time.sleep(0.1)
        raise _fail("client.managed.start_unconfirmed")

    def _clear_pending_tag(self, infohash: str) -> dict[str, Any]:
        remove = getattr(self._c, "torrents_remove_tags", None)
        if not callable(remove):
            raise _fail("client.qbittorrent.no_tag_removal")
        remove(tags="tow-pending", torrent_hashes=infohash.lower())
        for attempt in range(50):
            inspected = self.inspect_torrent(infohash)
            tags = {str(tag).strip().casefold() for tag in (inspected or {}).get("tags") or []}
            if "tow" in tags and "tow-pending" not in tags:
                return inspected or {}
            if attempt < 49:
                time.sleep(0.1)
        raise _fail("client.managed.pending_not_cleared")

    def _set_priorities_exact(
        self,
        infohash: str,
        source_files: tuple[TorrentFile, ...],
        selected_indices: set[int],
        root_name: str,
    ) -> dict[str, Any]:
        inspected = self._inspect_with_files(infohash)
        valid_source = {row.index for row in source_files if not row.is_pad}
        if selected_indices == valid_source:
            if any(row.is_pad or self._padding_like_name(row.path) for row in source_files):
                mapping = self._map_files(source_files, list(inspected.get("files") or []), root_name)
                all_client_ids = sorted(
                    int(row["index"]) for row in inspected.get("files") or [] if isinstance(row.get("index"), int)
                )
                selected_ids = sorted(mapping.values())
                self._c.torrents_file_priority(torrent_hash=infohash.lower(), file_ids=all_client_ids, priority=0)
                self._c.torrents_file_priority(torrent_hash=infohash.lower(), file_ids=selected_ids, priority=1)
                verified = self._inspect_with_files(infohash)
                priorities = {
                    int(row["index"]): int(row.get("priority") or 0)
                    for row in verified.get("files") or []
                    if isinstance(row.get("index"), int)
                }
                if any(priorities.get(index, 0) <= 0 for index in selected_ids):
                    raise _fail("client.managed.wrong_selection")
                return verified
            client_rows = [
                row
                for row in inspected.get("files") or []
                if isinstance(row.get("index"), int) and not self._padding_like_name(row.get("name"))
            ]
            if len(client_rows) != len(valid_source):
                raise _fail("client.managed.wrong_selection")
            ids = sorted(int(row["index"]) for row in client_rows)
            self._c.torrents_file_priority(torrent_hash=infohash.lower(), file_ids=ids, priority=1)
            verified = self._inspect_with_files(infohash)
            priorities = {
                int(row["index"]): int(row.get("priority") or 0)
                for row in verified.get("files") or []
                if isinstance(row.get("index"), int)
            }
            if any(priorities.get(index, 0) <= 0 for index in ids):
                raise _fail("client.managed.wrong_selection")
            return verified
        mapping = self._map_files(source_files, list(inspected.get("files") or []), root_name)
        all_ids = sorted(int(row["index"]) for row in inspected.get("files") or [] if isinstance(row.get("index"), int))
        if any(int(index) not in mapping for index in selected_indices):
            # Like add_torrent_selected: a clear refusal, not a bare KeyError.
            raise _fail("client.managed.bad_selection")
        selected_ids = sorted(mapping[int(index)] for index in selected_indices)
        if not selected_ids:
            raise _fail("client.managed.bad_selection")
        self._c.torrents_file_priority(torrent_hash=infohash.lower(), file_ids=all_ids, priority=0)
        self._c.torrents_file_priority(torrent_hash=infohash.lower(), file_ids=selected_ids, priority=1)
        verified = self._inspect_with_files(infohash)
        priorities = {
            int(row["index"]): int(row.get("priority") or 0)
            for row in verified.get("files") or []
            if isinstance(row.get("index"), int)
        }
        expected_ids = set(selected_ids)
        for client_index in all_ids:
            if (priorities.get(client_index, 0) > 0) != (client_index in expected_ids):
                raise _fail("client.managed.wrong_selection")
        return verified

    def add_torrent_selected(
        self,
        content: bytes,
        save_path: str | None,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
    ) -> dict[str, Any]:
        metadata = parse_torrent_metadata(content)
        if metadata.client_hash.casefold() != str(infohash).casefold():
            raise _fail("client.managed.hash_changed_add")
        destination = str(save_path or "").strip()
        if not destination:
            raise _fail("client.managed.no_folder")
        selected = {int(index) for index in selected_indices}
        valid = {row.index for row in metadata.files if not row.is_pad}
        if not selected or not selected.issubset(valid):
            raise _fail("client.managed.bad_selection")
        # G8: the owner's optional category and extra tags; "tow"/"tow-pending" stay first.
        extra_tags = [tag for tag in self.add_tags if tag not in {"tow", "tow-pending"}]
        category = str(self.add_category or "").strip()
        if category:
            with contextlib.suppress(Exception):  # it may exist already; the add reports real problems
                self._c.torrents_create_category(name=category)
        add_options: dict[str, Any] = {
            "torrent_files": content,
            "save_path": destination,
            "use_auto_torrent_management": False,
            "tags": ",".join(["tow", "tow-pending", *extra_tags]),
            **({"category": category} if category else {}),
            "is_stopped": True,
            "content_layout": "Original",
        }
        result = self._c.torrents_add(**add_options)
        self._api_ok(result, action="client.qbittorrent.action_add")
        owned_hash: str | None = None
        release_requested = False
        try:
            owned_hash, added = self._wait_for_ownership(
                infohash,
                visibility_error="client.managed.not_visible",
                ownership_error="client.managed.no_owner_mark",
                aliases=tuple(
                    value
                    for value in (
                        metadata.hash_v1,
                        metadata.hash_v2,
                        metadata.hash_v2[:40] if metadata.hash_v2 else None,
                    )
                    if value
                ),
            )
            if not paths_equal(str(added.get("save_path") or ""), destination):
                raise _fail("client.managed.wrong_folder")
            self._stop(owned_hash)
            self._wait_stopped(owned_hash)
            self._set_priorities_exact(owned_hash, metadata.files, selected, metadata.name)
            self._start(owned_hash)
            self._wait_started(owned_hash)
            release_requested = True
            return self._clear_pending_tag(owned_hash)
        except Exception:
            if owned_hash is not None and not release_requested:
                try:
                    self._stop(owned_hash)
                except Exception as cleanup_error:  # noqa: BLE001 - the original failure is re-raised; this must not hide it
                    _LOG.warning("cleanup after a failed client change did not finish: %s", cleanup_error)
            raise

    def configure_torrent_selection(
        self,
        content: bytes,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
        *,
        ensure_started: bool = False,
    ) -> dict[str, Any]:
        metadata = parse_torrent_metadata(content)
        valid_hashes = {metadata.client_hash.casefold()}
        if metadata.hash_v1:
            valid_hashes.add(metadata.hash_v1.casefold())
        if str(infohash).casefold() not in valid_hashes:
            raise _fail("client.managed.hash_changed_selection")
        before = self._inspect_with_files(infohash)
        pending = any(str(tag).casefold() == "tow-pending" for tag in before.get("tags") or [])
        previous = {
            int(row["index"]): int(row.get("priority") or 0)
            for row in before.get("files") or []
            if isinstance(row.get("index"), int)
        }
        was_stopped = str(before.get("state") or "").casefold().startswith(("stopped", "paused"))
        release_requested = False
        try:
            self._stop(infohash)
            self._wait_stopped(infohash)
            verified = self._set_priorities_exact(
                infohash,
                metadata.files,
                {int(index) for index in selected_indices},
                metadata.name,
            )
            if pending or not was_stopped or ensure_started:
                self._start(infohash)
                verified = self._wait_started(infohash)
            if pending:
                release_requested = True
                verified = self._clear_pending_tag(infohash)
            return verified
        except Exception:
            if release_requested:
                raise
            try:
                self._stop(infohash)
                for priority in sorted(set(previous.values())):
                    ids = sorted(index for index, value in previous.items() if value == priority)
                    if ids:
                        self._c.torrents_file_priority(torrent_hash=infohash.lower(), file_ids=ids, priority=priority)
                restored = self._inspect_with_files(infohash)
                restored_priorities = {
                    int(row["index"]): int(row.get("priority") or 0)
                    for row in restored.get("files") or []
                    if isinstance(row.get("index"), int)
                }
                if restored_priorities != previous:
                    raise _fail("client.managed.wrong_selection")
                if not was_stopped:
                    self._start(infohash)
                    self._wait_started(infohash)
            except Exception as cleanup_error:  # noqa: BLE001 - the original failure is re-raised; this must not hide it
                _LOG.warning("cleanup after a failed client change did not finish: %s", cleanup_error)
            raise

    def inspect_torrent(self, infohash: str) -> dict[str, Any] | None:
        rows = self._c.torrents_info(torrent_hashes=infohash.lower())
        if not rows:
            return None
        torrent = rows[0]
        files = self._c.torrents_files(torrent_hash=infohash.lower())
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
        raw_tags = getattr(torrent, "tags", "")
        if isinstance(raw_tags, str):
            normalized_tags = sorted({part.strip() for part in raw_tags.split(",") if part.strip()})
        else:
            normalized_tags = sorted({str(part).strip() for part in (raw_tags or []) if str(part).strip()})
        return {
            "hash": str(getattr(torrent, "hash", infohash) or infohash),
            "infohash_v1": str(getattr(torrent, "infohash_v1", "") or ""),
            "infohash_v2": str(getattr(torrent, "infohash_v2", "") or ""),
            "progress": getattr(torrent, "progress", 0.0),
            "downloaded": getattr(torrent, "downloaded", 0),
            "added_on": getattr(torrent, "added_on", None),
            "completion_on": getattr(torrent, "completion_on", None),
            "save_path": getattr(torrent, "save_path", None),
            "content_path": getattr(torrent, "content_path", None),
            "state": str(getattr(torrent, "state", "") or ""),
            "tags": normalized_tags,
            "files": normalized_files,
        }

    def set_location(self, infohash: str, save_path: str) -> str:
        dest = (save_path or "").strip()
        if not dest:
            raise _fail("client.managed.no_folder")
        self._c.torrents_set_location(location=dest, torrent_hashes=infohash.lower())
        return "ok"


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
