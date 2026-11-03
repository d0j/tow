"""Transmission (3.0+, 4.x): RPC over HTTP, ownership through torrent labels.

Checked against a live transmission-daemon 4.1.3 (RPC 19). Labels need Transmission 3.0+.
Magnet links are not supported: Transmission cannot fetch metadata without starting the
download, and TOW never lets a client download before the file selection is applied.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, ClassVar, cast

import httpx

from tow.clients.address import base_url
from tow.clients.managed import OWNER, PENDING, AddMarkError, ClientError, ManagedClient
from tow.clients.spec import TorrentClientAdapter

KIND = "transmission"
TITLE = "Transmission"
SECRETS_KEY = "transmission"
READY = True
DEFAULT_PORT = 9091
ORDER = 20
SHORT = "Transm."
# Texts are keys of the language files (client.transmission.*).
STEPS = (
    "client.transmission.step_open",
    "client.transmission.step_enable",
    "client.transmission.step_whitelist",
)
NOTE = "client.transmission.note"
DEFAULT_PATH = "/transmission/rpc"
SESSION_HEADER = "X-Transmission-Session-Id"
# Labels arrived in RPC 16 (Transmission 3.0).
MIN_RPC_VERSION = 16
_LOG = logging.getLogger(__name__)

_FIELDS = [
    "hashString",
    "name",
    "status",
    "error",
    "errorString",
    "percentDone",
    "downloadedEver",
    "addedDate",
    "doneDate",
    "downloadDir",
    "labels",
    "files",
    "fileStats",
]
# status: 0 stopped, 1 check wait, 2 checking, 3 download wait, 4 downloading, 5 seed wait, 6 seeding
_STATES = {1: "checkingDL", 2: "checkingDL", 3: "queuedDL", 4: "downloading", 5: "queuedUP", 6: "uploading"}


def _state_of(row: dict[str, Any], progress: float = 0.0) -> tuple[str, bool]:
    """RPC status and local health are independent enums, not truthy numeric values.

    Transmission reports status=0 for a stopped torrent even while error=3 describes a
    local file failure. Missing, boolean, string or unknown status is no stop evidence.
    """
    raw_status, error = row.get("status"), row.get("error")
    status = raw_status if type(raw_status) is int and 0 <= raw_status <= 6 else None
    valid_error = type(error) is int and 0 <= error <= 3
    if valid_error and error == 3:
        return "error", status == 0
    if status is None or not valid_error:
        return "unknown", False
    if status == 0:
        return ("stoppedUP" if progress >= 1 else "stoppedDL"), True
    return _STATES[status], False


def _labels(row: dict[str, Any]) -> list[str]:
    return sorted({str(label) for label in row.get("labels") or [] if str(label).strip()})


class TransmissionClient(ManagedClient):
    title = "Transmission"
    kind = KIND
    client_kind = KIND

    capabilities: ClassVar[dict[str, bool]] = {
        **ManagedClient.capabilities,
        "magnet_metadata": False,
        "recheck_files": True,
    }

    def __init__(
        self,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        path: str = DEFAULT_PATH,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        url = base_url(host, port)
        if not url.split("://", 1)[1].count("/"):
            url += "/" + (path or DEFAULT_PATH).strip("/")
        self._url = url
        self._session = ""
        auth = httpx.BasicAuth(username, password) if username or password else None
        # trust_env=False: a system or HTTP_PROXY proxy would receive the Authorization header.
        self._http = httpx.Client(timeout=10.0, auth=auth, transport=transport, follow_redirects=False, trust_env=False)

    def _rpc(self, method: str, **arguments: Any) -> dict[str, Any]:
        for _attempt in range(3):
            try:
                response = self._http.post(
                    self._url, json={"method": method, "arguments": arguments}, headers={SESSION_HEADER: self._session}
                )
            except httpx.HTTPError as exc:
                raise self._fail("client.transmission.no_connection", error=type(exc).__name__) from exc
            if response.status_code == 409 and response.headers.get(SESSION_HEADER):
                self._session = response.headers[SESSION_HEADER]
                continue
            if response.status_code == 401:
                raise self._fail("client.transmission.bad_login")
            if response.status_code == 403:
                raise self._fail("client.transmission.forbidden")
            if response.status_code == 421:
                raise self._fail("client.transmission.host_not_allowed")
            if response.status_code == 404:
                raise self._fail("client.transmission.no_rpc")
            if response.status_code != 200:
                raise self._fail("client.managed.http_error", code=response.status_code)
            try:
                body = response.json()
            except ValueError as exc:
                raise self._fail("client.transmission.not_rpc") from exc
            if body.get("result") != "success":
                raise self._fail("client.managed.rpc_refused", method=method, answer=str(body.get("result")))
            return body.get("arguments") or {}
        raise self._fail("client.transmission.no_session")

    def ping(self) -> str:
        info = self._rpc("session-get", fields=["version", "rpc-version"])
        rpc = int(info.get("rpc-version") or 0)
        if rpc < MIN_RPC_VERSION:
            raise self._fail("client.transmission.too_old", version=str(info.get("version")))
        return f"{info.get('version')} rpc {rpc}"

    def has_any_torrent(self) -> bool:
        """The client lists at least one torrent (one still loading its list after a start lists none)."""
        return bool(self._rpc("torrent-get", fields=["id"]).get("torrents"))

    def _get(self, infohash: str, fields: list[str] = _FIELDS) -> dict[str, Any] | None:
        rows = self._rpc("torrent-get", ids=[infohash.lower()], fields=fields).get("torrents") or []
        wanted = infohash.casefold()
        for row in rows:
            if str(row.get("hashString") or "").casefold() == wanted:
                return cast(dict[str, Any], row)
        return None

    def _owner_tags(self, infohash: str) -> list[str] | None:
        row = self._get(infohash, ["hashString", "labels"])
        return None if row is None else _labels(row)

    def inspect_torrent(self, infohash: str) -> dict[str, Any] | None:
        row = self._get(infohash)
        if row is None:
            return None
        progress = float(row.get("percentDone") or 0)
        state, stopped = _state_of(row, progress)
        files = []
        stats = row.get("fileStats") or []
        for index, item in enumerate(row.get("files") or []):
            stat = stats[index] if index < len(stats) else {}
            wanted = stat.get("wanted")
            priority = int(wanted) if isinstance(wanted, (bool, int)) and wanted in (0, 1) else None
            length = int(item.get("length") or 0)
            done = int(item.get("bytesCompleted") or 0)
            files.append(
                {
                    "index": index,
                    "name": str(item.get("name") or ""),
                    "size": length,
                    "progress": (done / length) if length else 1.0,
                    "priority": priority,
                }
            )
        save_path = str(row.get("downloadDir") or "")
        added = int(row.get("addedDate") or 0)
        done_at = int(row.get("doneDate") or 0)
        return {
            "hash": str(row.get("hashString") or ""),
            "infohash_v1": str(row.get("hashString") or ""),
            "infohash_v2": "",
            "progress": progress,
            "downloaded": int(row.get("downloadedEver") or 0),
            "added_on": added or None,
            "completion_on": done_at or None,
            "save_path": save_path,
            "content_path": f"{save_path.rstrip('/\\')}/{row.get('name')}" if save_path else None,
            "state": state,
            "stopped": stopped,
            "tags": _labels(row),
            "files": files,
        }

    def _add_stopped(self, content: bytes, save_path: str, labels: list[str]) -> None:
        result = self._rpc(
            "torrent-add",
            metainfo=base64.b64encode(content).decode("ascii"),
            **{"download-dir": save_path},
            paused=True,
            labels=labels,
        )
        if "torrent-duplicate" in result:
            raise self._fail("client.managed.already_there")
        added = result.get("torrent-added") or {}
        if added.get("hashString"):
            # Older Transmission ignores labels on add; set them before anything else.
            identity = str(added["hashString"])
            try:
                self._set_labels(identity, labels)
            except Exception as error:
                # Preserve the client's confirmed identity for marking recovery.
                raise AddMarkError(identity, error) from error

    def _set_wanted(self, infohash: str, wanted: set[int], all_ids: list[int]) -> None:
        unwanted = [index for index in all_ids if index not in wanted]
        arguments: dict[str, Any] = {"ids": [infohash.lower()]}
        if wanted:
            arguments["files-wanted"] = sorted(wanted)
        if unwanted:
            arguments["files-unwanted"] = unwanted
        self._rpc("torrent-set", **arguments)

    def _defers_initial_check(self, info: dict[str, Any]) -> bool:
        """A paused add verifies the data it found; stopping it cancels that verification, and a
        large one outlasts any wait: the add is handed over before its selection."""
        return str(info.get("state") or "").casefold().startswith("checking")

    def _verification_state(self, infohash: str) -> dict[str, Any] | None:
        row = self._get(infohash, ["hashString", "status", "error", "labels"])
        if row is None:
            return None
        state, stopped = _state_of(row)
        return {"hash": str(row.get("hashString") or ""), "state": state, "stopped": stopped, "tags": _labels(row)}

    def _wait_initial_verification(self, added: dict[str, Any]) -> dict[str, Any]:
        if str(added.get("state") or "").casefold().startswith("checking"):
            return self._wait(
                str(added["hash"]),
                lambda info: not str(info.get("state") or "").casefold().startswith("checking"),
                "client.transmission.check_in_progress",
                polls=self.START_POLLS,
                timeout=self.START_WAIT_SEC,
                read=self._verification_state,
            )
        return added

    def _ready_for_selection(self, before: dict[str, Any]) -> dict[str, Any]:
        if str(before.get("state") or "").casefold().startswith("checking"):
            self._wait_initial_verification(before)
            return self._inspect_with_files(str(before["hash"]))
        return before

    def _stop_after_failure(self, infohash: str, *, error: Exception | None = None) -> None:
        if not isinstance(error, ClientError) or error.code != "client.transmission.check_in_progress":
            super()._stop_after_failure(infohash, error=error)
            return
        try:
            info = self._verification_state(infohash)
            if {OWNER, PENDING}.issubset(self._tags(info)) and str((info or {}).get("state")).startswith("checking"):
                return  # the newly added paused task may finish checking; no start was requested
        except Exception:  # noqa: BLE001 - an unreadable state still takes the normal safe-stop path
            _LOG.debug("initial verification status unreadable; using safe-stop cleanup")
        super()._stop_after_failure(infohash, error=error)

    def _stop(self, infohash: str) -> None:
        self._rpc("torrent-stop", ids=[infohash.lower()])

    def _start(self, infohash: str) -> None:
        self._rpc("torrent-start", ids=[infohash.lower()])

    def _recheck(self, infohash: str) -> None:
        self._rpc("torrent-verify", ids=[infohash.lower()])

    def _other_torrents(self, current_hash: str, expected_save_path: str) -> list[dict[str, Any]]:
        rows = self._rpc("torrent-get", fields=["hashString", "status", "error", "downloadDir"]).get("torrents")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise self._fail("client.managed.repair_changed")
        listing = [
            {
                "hash": row.get("hashString"),
                "save_path": row.get("downloadDir"),
                "state": _state_of(row)[0],
                "stopped": _state_of(row)[1],
            }
            for row in rows
        ]
        found = []
        for candidate in self._repair_candidates(listing, current_hash, expected_save_path):
            info = self.inspect_torrent(str(candidate["hash"]))
            found.append(self._repair_other_readback(candidate, info))
        return found

    def _set_labels(self, infohash: str, labels: list[str]) -> None:
        self._rpc("torrent-set", ids=[infohash.lower()], labels=labels)

    def _move(self, infohash: str, save_path: str) -> None:
        self._rpc("torrent-set-location", ids=[infohash.lower()], location=save_path, move=True)


def from_secrets(secrets: dict[str, Any]) -> TorrentClientAdapter:
    block = secrets.get(SECRETS_KEY) or {}
    if not block.get("host"):
        raise ClientError("client.managed.no_address", prefix=TITLE)
    return TransmissionClient(
        host=str(block["host"]),
        port=int(block.get("port") or DEFAULT_PORT),
        username=str(block.get("username") or ""),
        password=str(block.get("password") or ""),
        path=str(block.get("path") or DEFAULT_PATH),
    )
