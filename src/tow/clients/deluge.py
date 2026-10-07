"""Deluge 2.x through its Web UI (JSON-RPC), ownership through the bundled Label plugin.

Checked against a live Deluge 2.2.0 (libtorrent 2.1). TOW turns the Label plugin on when it
is off: without a label TOW could not tell its own torrents from yours. Deluge keeps one
label per torrent, so the extra tags/category from the config are not applied here.
"""

from __future__ import annotations

import base64
from typing import Any, ClassVar

import httpx

from tow.clients.managed import OWNER, PENDING, ClientError, ManagedClient
from tow.clients.spec import ClientField, TorrentClientAdapter
from tow.clients.transmission import base_url
from tow.torrent import MAX_TORRENT_BYTES, parse_magnet_hashes, parse_torrent_metadata

KIND = "deluge"
TITLE = "Deluge"
SECRETS_KEY = "deluge"
READY = True
DEFAULT_PORT = 8112
ORDER = 30
SHORT = "Deluge"
# Texts are keys of the language files (client.deluge.*, shared field labels client.fields.*).
FIELDS = (
    ClientField("host", "client.fields.host", "127.0.0.1"),
    ClientField("port", "client.fields.port"),
    ClientField("password", "client.deluge.password_label"),
)
STEPS = (
    "client.deluge.step_enable",
    "client.deluge.step_password",
    "client.deluge.step_save",
)
NOTE = "client.deluge.note"

_KEYS = [
    "hash",
    "name",
    "state",
    "progress",
    "all_time_download",
    "time_added",
    "completed_time",
    "download_location",
    "files",
    "file_priorities",
    "file_progress",
    "label",
]
_STATES = {
    "downloading": "downloading",
    "seeding": "uploading",
    "checking": "checkingDL",
    "allocating": "checkingDL",
    "moving": "moving",
    "error": "error",
}


def _label_tags(row: dict[str, Any]) -> list[str]:
    """Deluge has one label per torrent: "tow-pending" carries both of TOW's marks."""
    label = str(row.get("label") or "").strip()
    return [OWNER, PENDING] if label == PENDING else ([label] if label else [])


class DelugeClient(ManagedClient):
    title = "Deluge"
    kind = KIND
    client_kind = KIND

    capabilities: ClassVar[dict[str, bool]] = {
        **ManagedClient.capabilities,
        "magnet_metadata": True,
        "metadata_preview": True,
    }
    MAGNET_TIMEOUT = 45
    # A dry run (preview) changes nothing, not even which daemon Deluge Web is attached to.
    read_only = False

    def __init__(self, host: str, port: int, password: str, *, transport: httpx.BaseTransport | None = None) -> None:
        url = base_url(host, port)
        self._url = url.rstrip("/") + "/json"
        self._password = password
        self._http = httpx.Client(timeout=15.0, transport=transport, follow_redirects=False)
        self._id = 0
        self._ready = False
        self._labels_ready = False
        self._metadata_preview = False

    def _post(self, method: str, params: list[Any], timeout: float | None = None) -> dict[str, Any]:
        self._id += 1
        try:
            response = self._http.post(
                self._url,
                json={"method": method, "params": params, "id": self._id},
                timeout=timeout or self._http.timeout,
            )
        except httpx.HTTPError as exc:
            raise self._fail("client.deluge.no_connection", error=type(exc).__name__) from exc
        if response.status_code == 404:
            raise self._fail("client.deluge.no_web")
        if response.status_code != 200:
            raise self._fail("client.managed.http_error", code=response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise self._fail("client.deluge.not_web") from exc
        if not isinstance(body, dict) or ("result" not in body and not body.get("error")):
            # Not Deluge's answer (a proxy, a Web UI still starting): never "no such torrent".
            raise self._fail("client.deluge.not_web")
        return body

    def _raw(self, method: str, *params: Any, timeout: float | None = None) -> Any:
        body = self._post(method, list(params), timeout)
        error = body.get("error")
        if error:
            message = str(error.get("message") if isinstance(error, dict) else error)
            code = error.get("code") if isinstance(error, dict) else None
            raise _RpcError(message, code)
        return body.get("result")

    def _connect(self) -> None:
        """Log in and attach Deluge Web to its daemon (read-only for the client's settings)."""
        if not self._raw("auth.login", self._password):
            raise self._fail("client.deluge.bad_password")
        if not self._raw("web.connected"):
            if self.read_only or self._metadata_preview:
                raise self._fail("client.deluge.not_attached_preview")
            hosts = self._raw("web.get_hosts") or []
            online = [
                host[0]
                for host in hosts
                if (self._raw("web.get_host_status", host[0]) or [None, ""])[1] in ("Online", "Connected")
            ]
            if not online:
                raise self._fail("client.deluge.no_daemon")
            self._raw("web.connect", online[0])
        self._ready = True

    def _ensure_labels(self) -> None:
        """Turn on the bundled Label plugin and create TOW's labels - only when TOW adds or
        marks a torrent, never for a connection check or a dry run."""
        if self._labels_ready:
            return
        plugins = self._call("core.get_enabled_plugins") or []
        if "Label" not in plugins:
            self._call("core.enable_plugin", "Label")
            # A build without the plugin answers False and enables nothing: without labels TOW
            # could not mark what it adds, so nothing is added.
            if "Label" not in (self._call("core.get_enabled_plugins") or []):
                raise self._fail("client.deluge.no_label_plugin")
        labels = set(self._call("label.get_labels") or [])
        for label in (OWNER, PENDING):
            if label not in labels:
                self._call("label.add", label)
        self._labels_ready = True

    def _call(self, method: str, *params: Any, timeout: float | None = None) -> Any:
        for attempt in range(2):
            if not self._ready:
                self._connect()
            try:
                return self._raw(method, *params, timeout=timeout)
            except _RpcError as exc:
                # 1: session expired, 2: method unknown until the daemon is (re)connected.
                if exc.code in (1, 2) and attempt == 0:
                    self._ready = False
                    continue
                raise self._fail("client.managed.rpc_refused", method=method, answer=exc.message) from exc
        raise self._fail("client.deluge.no_answer", method=method)

    def ping(self) -> str:
        self._ready = False
        version = self._call("daemon.get_version")
        return f"{version} libtorrent {self._call('core.get_libtorrent_version')}"

    def has_any_torrent(self) -> bool:
        """The client lists at least one torrent (one still loading its list after a start lists none)."""
        return bool(self._call("core.get_session_state"))

    def _owner_tags(self, infohash: str) -> list[str] | None:
        row = self._call("core.get_torrent_status", infohash.lower(), ["hash", "label"])
        return _label_tags(row) if row and row.get("hash") else None

    def inspect_torrent(self, infohash: str) -> dict[str, Any] | None:
        row = self._call("core.get_torrent_status", infohash.lower(), _KEYS)
        if not row or not row.get("hash"):
            return None
        progress = float(row.get("progress") or 0) / 100
        state_name = str(row.get("state") or "").casefold()
        if state_name == "paused":
            state = "stoppedUP" if progress >= 1 else "stoppedDL"
        elif state_name == "queued":
            state = "queuedUP" if progress >= 1 else "queuedDL"
        else:
            state = _STATES.get(state_name, "unknown")
        priorities = row.get("file_priorities") or []
        done = row.get("file_progress") or []
        files = []
        for item in row.get("files") or []:
            index = int(item.get("index") or 0)
            files.append(
                {
                    "index": index,
                    "name": str(item.get("path") or ""),
                    "size": int(item.get("size") or 0),
                    "progress": float(done[index]) if 0 <= index < len(done) else 0.0,
                    "priority": int(priorities[index]) if 0 <= index < len(priorities) else None,
                }
            )
        tags = _label_tags(row)
        save_path = str(row.get("download_location") or "")
        return {
            "hash": str(row["hash"]),
            "infohash_v1": str(row["hash"]),
            "infohash_v2": "",
            "progress": progress,
            "downloaded": int(row.get("all_time_download") or 0),
            "added_on": int(row.get("time_added") or 0) or None,
            "completion_on": int(row.get("completed_time") or 0) or None,
            "save_path": save_path,
            "content_path": f"{save_path.rstrip('/\\')}/{row.get('name')}" if save_path else None,
            "state": state,
            "tags": tags,
            "files": files,
        }

    def _prepare_add(self) -> None:
        self._ensure_labels()  # a torrent added before its label exists could stay unmarked

    def _add_stopped(self, content: bytes, save_path: str, labels: list[str]) -> None:
        torrent_id = self._call(
            "core.add_torrent_file",
            "tow.torrent",
            base64.b64encode(content).decode("ascii"),
            {"add_paused": True, "download_location": save_path},
        )
        if not torrent_id:
            raise self._fail("client.deluge.add_refused")
        self._set_labels(str(torrent_id), labels)

    def _set_wanted(self, infohash: str, wanted: set[int], all_ids: list[int]) -> None:
        priorities = [4 if index in wanted else 0 for index in range(max(all_ids, default=-1) + 1)]
        self._call("core.set_torrent_options", [infohash.lower()], {"file_priorities": priorities})

    def _stop(self, infohash: str) -> None:
        self._call("core.pause_torrents", [infohash.lower()])

    def _start(self, infohash: str) -> None:
        self._call("core.resume_torrents", [infohash.lower()])

    def _set_labels(self, infohash: str, labels: list[str]) -> None:
        self._ensure_labels()
        folded = {label.casefold() for label in labels}
        label = PENDING if PENDING in folded else (OWNER if OWNER in folded else "")
        self._call("label.set_torrent", infohash.lower(), label)

    def _move(self, infohash: str, save_path: str) -> None:
        self._call("core.move_storage", [infohash.lower()], save_path)

    def materialize_magnet(self, magnet_url: str, save_path: str | None, infohash: str) -> bytes:
        """Metadata only: Deluge fetches it from peers WITHOUT adding the torrent (no payload bytes)."""
        hashes = parse_magnet_hashes(str(magnet_url or ""))
        if hashes is None:
            raise self._fail("client.deluge.magnet_invalid")
        btih, btmh = hashes
        expected = str(infohash or "").strip().upper()
        if expected not in btih | btmh:
            raise self._fail("client.deluge.magnet_hash_mismatch")
        if not btih:
            raise self._fail("client.deluge.magnet_v2_only")
        if self.read_only:
            raise self._fail("content.magnet_unsupported")
        result = self._call(
            "core.prefetch_magnet_metadata", magnet_url, self.MAGNET_TIMEOUT, timeout=self.MAGNET_TIMEOUT + 15
        )
        encoded = result[1] if isinstance(result, list) and len(result) == 2 else None
        if not encoded:
            raise self._fail("client.deluge.magnet_timeout", seconds=self.MAGNET_TIMEOUT)
        if not isinstance(encoded, (str, bytes)) or len(encoded) > ((MAX_TORRENT_BYTES + 2) // 3) * 4:
            raise self._fail("content.too_large")
        try:
            info = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise self._fail("content.magnet_failed") from exc
        content = _torrent_with_trackers(info, magnet_url)
        metadata = parse_torrent_metadata(content)
        if metadata.hash_v1 not in btih or (btmh and metadata.hash_v2 not in btmh):
            raise self._fail("client.deluge.magnet_data_mismatch")
        return content

    def preview_magnet(self, magnet_url: str) -> bytes:
        hashes = parse_magnet_hashes(magnet_url)
        if hashes is None:
            raise self._fail("client.deluge.magnet_invalid")
        btih, btmh = hashes
        # Preview must not attach the Web UI to a different daemon or enable Label.
        self._metadata_preview = True
        try:
            return self.materialize_magnet(magnet_url, None, next(iter(btih or btmh)))
        except ClientError as exc:
            # A daemon may echo a magnet URI (including a private tracker passkey) in
            # its error. Keep safe connection/validation codes, not arbitrary RPC text.
            if exc.code == "client.managed.rpc_refused":
                raise self._fail("content.magnet_failed") from exc
            raise
        finally:
            self._metadata_preview = False


def _bstr(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


def _torrent_with_trackers(info: bytes, magnet_url: str) -> bytes:
    """A .torrent from the info dictionary plus the magnet's trackers (``tr=``): without them a
    private tracker's torrent would never find peers (DHT is off there)."""
    from urllib.parse import parse_qs, urlsplit

    trackers = [
        url
        for url in dict.fromkeys(parse_qs(urlsplit(magnet_url).query).get("tr", []))
        if url.lower().startswith(("http://", "https://", "udp://"))
    ]
    if not trackers:
        return b"d4:info" + info + b"e"
    tiers = b"l" + b"".join(b"l" + _bstr(url.encode("utf-8")) + b"e" for url in trackers) + b"e"
    return b"d8:announce" + _bstr(trackers[0].encode("utf-8")) + b"13:announce-list" + tiers + b"4:info" + info + b"e"


class _RpcError(Exception):
    def __init__(self, message: str, code: object) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def from_secrets(secrets: dict[str, Any]) -> TorrentClientAdapter:
    block = secrets.get(SECRETS_KEY) or {}
    if not block.get("host"):
        raise ClientError("client.managed.no_address", prefix=TITLE)
    return DelugeClient(
        host=str(block["host"]),
        port=int(block.get("port") or DEFAULT_PORT),
        password=str(block.get("password") or ""),
    )
