from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

MAX_TORRENT_BYTES = 32 * 1024 * 1024
MAX_BENCODE_DEPTH = 64
MAX_BENCODE_VALUES = 200_000
MAX_FILES = 20_000
MAX_PATH_DEPTH = 64
MAX_COMPONENT_BYTES = 255
MAX_PATH_CHARS = 4096

_WINDOWS_DEVICE = re.compile(r"^(?:con|prn|aux|nul|clock\$|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)
_WINDOWS_FORBIDDEN = str.maketrans(dict.fromkeys('<>:"|?*', "_"))


def parse_magnet_hashes(value: str) -> tuple[frozenset[str], frozenset[str]] | None:
    """Return unique BTIH and BEP 52 SHA-256 hashes from a magnet URI."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.casefold() != "magnet":
        return None
    btih: set[str] = set()
    btmh: set[str] = set()
    for xt in parse_qs(parsed.query).get("xt") or []:
        match = re.fullmatch(r"urn:btih:([0-9a-fA-F]{40})", xt, re.IGNORECASE)
        if match:
            btih.add(match.group(1).upper())
            continue
        match = re.fullmatch(r"urn:btih:([A-Z2-7]{32})", xt, re.IGNORECASE)
        if match:
            try:
                btih.add(base64.b32decode(match.group(1).upper()).hex().upper())
            except ValueError, TypeError:
                return None
            continue
        match = re.fullmatch(r"urn:btmh:1220([0-9a-fA-F]{64})", xt, re.IGNORECASE)
        if match:
            btmh.add(match.group(1).upper())
    if len(btih) > 1 or len(btmh) > 1 or not (btih or btmh):
        return None
    return frozenset(btih), frozenset(btmh)


@dataclass(frozen=True, slots=True)
class TorrentFile:
    index: int
    path: str
    size: int
    is_pad: bool = False


@dataclass(frozen=True, slots=True)
class TorrentMetadata:
    name: str
    infohash: str
    client_hash: str
    hash_v1: str | None
    hash_v2: str | None
    files: tuple[TorrentFile, ...]
    is_multi: bool
    meta_version: int | None


class _Decoder:
    def __init__(self, data: bytes) -> None:
        if not data or len(data) > MAX_TORRENT_BYTES:
            raise ValueError("torrent size is invalid")
        self.data = data
        self.values = 0

    def parse(self, pos: int = 0, depth: int = 0) -> tuple[Any, int]:
        if depth > MAX_BENCODE_DEPTH:
            raise ValueError("bencode nesting is too deep")
        self.values += 1
        if self.values > MAX_BENCODE_VALUES:
            raise ValueError("bencode contains too many values")
        if pos >= len(self.data):
            raise ValueError("truncated bencode")
        marker = self.data[pos : pos + 1]
        if marker == b"i":
            end = self.data.find(b"e", pos + 1)
            if end < 0:
                raise ValueError("truncated bencode integer")
            raw = self.data[pos + 1 : end]
            if not raw or raw == b"-0" or raw.startswith(b"+"):
                raise ValueError("invalid bencode integer")
            digits = raw[1:] if raw.startswith(b"-") else raw
            if not digits.isdigit() or (len(digits) > 1 and digits.startswith(b"0")):
                raise ValueError("non-canonical bencode integer")
            return int(raw), end + 1
        if marker == b"l":
            out: list[Any] = []
            pos += 1
            while True:
                if pos >= len(self.data):
                    raise ValueError("truncated bencode list")
                if self.data[pos : pos + 1] == b"e":
                    return out, pos + 1
                value, pos = self.parse(pos, depth + 1)
                out.append(value)
        if marker == b"d":
            mapping: dict[bytes, Any] = {}
            previous: bytes | None = None
            pos += 1
            while True:
                if pos >= len(self.data):
                    raise ValueError("truncated bencode dictionary")
                if self.data[pos : pos + 1] == b"e":
                    return mapping, pos + 1
                key, pos = self._string(pos)
                if previous is not None and key <= previous:
                    raise ValueError("duplicate or unsorted bencode dictionary key")
                previous = key
                value, pos = self.parse(pos, depth + 1)
                mapping[key] = value
        return self._string(pos)

    def _string(self, pos: int) -> tuple[bytes, int]:
        colon = self.data.find(b":", pos)
        if colon < 0:
            raise ValueError("truncated bencode string length")
        raw_len = self.data[pos:colon]
        if not raw_len or not raw_len.isdigit() or (len(raw_len) > 1 and raw_len.startswith(b"0")):
            raise ValueError("invalid bencode string length")
        length = int(raw_len)
        start = colon + 1
        end = start + length
        if end > len(self.data):
            raise ValueError("truncated bencode string")
        return self.data[start:end], end


def _decode_text(raw: bytes, *, what: str) -> str:
    if not isinstance(raw, bytes) or not raw:
        raise ValueError(f"torrent {what} is empty")
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            value = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if value:
            return value
    raise ValueError(f"torrent {what} cannot be decoded")


def _validate_component(raw: bytes) -> str:
    """Reject only components that could escape the torrent root or are unrepresentable.

    Characters that are merely illegal in Windows file names (``: ? * " < > |``,
    trailing dots/spaces, device names) are legal in torrents; the client sanitizes
    them on disk and ``windows_path_key`` matches both spellings. TOW never writes
    these paths itself.
    """
    if len(raw) > MAX_COMPONENT_BYTES:
        raise ValueError("torrent path component is too long")
    value = unicodedata.normalize("NFC", _decode_text(raw, what="path component"))
    if not value.strip(" ."):
        raise ValueError("torrent contains an unsafe path component")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or any(ch in value for ch in "/\\"):
        raise ValueError("torrent contains an unsafe path component")
    return value


def windows_path_key(path: str) -> str:
    """Comparison key that survives the client's Windows file-name sanitizing."""
    parts = []
    for part in unicodedata.normalize("NFC", str(path or "")).replace("\\", "/").split("/"):
        if part in {"", "."}:
            continue
        part = part.translate(_WINDOWS_FORBIDDEN).rstrip(" .") or "_"
        if _WINDOWS_DEVICE.fullmatch(part):
            part += "_"
        parts.append(part)
    return "/".join(parts).casefold()


def _path(parts: list[bytes]) -> str:
    if not parts or len(parts) > MAX_PATH_DEPTH:
        raise ValueError("torrent path depth is invalid")
    value = "/".join(_validate_component(part) for part in parts)
    if len(value) > MAX_PATH_CHARS:
        raise ValueError("torrent path is too long")
    return value


def _non_negative_int(value: Any, *, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"torrent {what} is invalid")
    return value


def _raw_info_span(data: bytes) -> tuple[dict[bytes, Any], dict[bytes, Any], int, int]:
    decoder = _Decoder(data)
    if data[:1] != b"d":
        raise ValueError("torrent root must be a dictionary")
    root: dict[bytes, Any] = {}
    previous: bytes | None = None
    info_start = info_end = -1
    pos = 1
    while True:
        if pos >= len(data):
            raise ValueError("truncated torrent root")
        if data[pos : pos + 1] == b"e":
            pos += 1
            break
        key, pos = decoder._string(pos)
        if previous is not None and key <= previous:
            raise ValueError("duplicate or unsorted torrent root key")
        previous = key
        start = pos
        value, pos = decoder.parse(pos, 1)
        if key == b"info":
            info_start, info_end = start, pos
        root[key] = value
    if pos != len(data):
        raise ValueError("torrent contains trailing data")
    info = root.get(b"info")
    if not isinstance(info, dict) or info_start < 0:
        raise ValueError("torrent has no info dictionary")
    return root, info, info_start, info_end


def _v1_files(info: dict[bytes, Any], name: str) -> tuple[list[TorrentFile], bool]:
    files_value = info.get(b"files")
    if files_value is None:
        attr = info.get(b"attr", b"")
        if attr is not None and not isinstance(attr, bytes):
            raise ValueError("torrent file attr is invalid")
        if b"l" in (attr or b"") or b"symlink path" in info:
            raise ValueError("torrent symlink entries are not supported")
        size = _non_negative_int(info.get(b"length"), what="length")
        return [TorrentFile(index=0, path=name, size=size, is_pad=b"p" in (attr or b""))], False
    if b"length" in info:
        raise ValueError("torrent info contains both length and files")
    if not isinstance(files_value, list) or not files_value:
        raise ValueError("torrent files list is empty or invalid")
    if len(files_value) > MAX_FILES:
        raise ValueError("torrent contains too many files")
    out: list[TorrentFile] = []
    for index, row in enumerate(files_value):
        if not isinstance(row, dict):
            # Malformed input, not a programming error: callers catch ValueError for "not a torrent".
            raise ValueError("torrent file entry is invalid")  # noqa: TRY004
        attr = row.get(b"attr", b"")
        if attr is not None and not isinstance(attr, bytes):
            raise ValueError("torrent file attr is invalid")
        if b"l" in (attr or b"") or b"symlink path" in row:
            raise ValueError("torrent symlink entries are not supported")
        is_pad = b"p" in (attr or b"")
        raw_parts = row.get(b"path.utf-8") or row.get(b"path")
        if raw_parts is None and is_pad:
            path = f".pad/{index}"
        elif isinstance(raw_parts, list) and all(isinstance(p, bytes) for p in raw_parts):
            path = _path(raw_parts)
        else:
            raise ValueError("torrent file path is invalid")
        out.append(
            TorrentFile(
                index=index,
                path=path,
                size=_non_negative_int(row.get(b"length"), what="file length"),
                is_pad=is_pad,
            )
        )
    return out, True


def _v2_files(info: dict[bytes, Any]) -> tuple[list[TorrentFile], bool]:
    tree = info.get(b"file tree")
    if not isinstance(tree, dict) or not tree:
        raise ValueError("v2 torrent has no file tree")
    out: list[TorrentFile] = []

    def walk(node: dict[bytes, Any], parts: list[bytes]) -> None:
        if len(parts) > MAX_PATH_DEPTH:
            raise ValueError("torrent path depth is invalid")
        if b"" in node and len(node) != 1:
            raise ValueError("v2 torrent path is both a file and a directory")
        for key, value in node.items():
            if key == b"":
                if not parts or not isinstance(value, dict):
                    raise ValueError("v2 torrent leaf is invalid")
                attr = value.get(b"attr", b"")
                if attr is not None and not isinstance(attr, bytes):
                    raise ValueError("torrent file attr is invalid")
                if b"l" in (attr or b"") or b"symlink path" in value:
                    raise ValueError("v2 symlink entries are not supported")
                out.append(
                    TorrentFile(
                        index=len(out),
                        path=_path(parts),
                        size=_non_negative_int(value.get(b"length"), what="file length"),
                        is_pad=b"p" in (attr or b""),
                    )
                )
            else:
                if not isinstance(value, dict):
                    raise ValueError("v2 torrent file tree is invalid")
                walk(value, [*parts, key])
            if len(out) > MAX_FILES:
                raise ValueError("torrent contains too many files")

    walk(tree, [])
    if not out:
        raise ValueError("v2 torrent file tree is empty")
    return out, len(out) != 1


def _validate_hybrid_alignment(v1_files: list[TorrentFile], v2_files: list[TorrentFile], piece_length: int) -> None:
    v1_index = 0
    offset = 0
    for v2_index, v2_file in enumerate(v2_files):
        padding = 0
        while v1_index < len(v1_files) and v1_files[v1_index].is_pad:
            padding += v1_files[v1_index].size
            v1_index += 1
        expected_padding = 0 if v2_index == 0 else (-offset) % piece_length
        if padding != expected_padding or v1_index >= len(v1_files):
            raise ValueError("hybrid torrent has invalid v1 padding alignment")
        v1_file = v1_files[v1_index]
        if (v1_file.path, v1_file.size) != (v2_file.path, v2_file.size):
            raise ValueError("hybrid torrent v1/v2 file trees disagree")
        offset += padding + v1_file.size
        v1_index += 1
    if v1_index != len(v1_files):
        raise ValueError("hybrid torrent has trailing v1 padding or files")


def _piece_layer_root(layer: bytes, count: int, piece_length: int) -> bytes:
    hashes = [layer[index : index + 32] for index in range(0, len(layer), 32)]
    if len(hashes) != count:
        raise ValueError("v2 piece layer has an invalid hash count")
    padding_hash = bytes(32)
    blocks_per_piece = piece_length // (16 * 1024)
    while blocks_per_piece > 1:
        padding_hash = hashlib.sha256(padding_hash + padding_hash).digest()
        blocks_per_piece //= 2
    target = 1 << (count - 1).bit_length()
    hashes.extend([padding_hash] * (target - count))
    while len(hashes) > 1:
        hashes = [hashlib.sha256(hashes[index] + hashes[index + 1]).digest() for index in range(0, len(hashes), 2)]
    return hashes[0]


def _piece_length(info: dict[bytes, Any]) -> int | None:
    """The ``piece length`` integer, or None when absent or not an integer (bool excluded)."""
    value = info.get(b"piece length")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _validate_v1_pieces(info: dict[bytes, Any], files: list[TorrentFile]) -> None:
    """The v1 ``pieces`` string must hold one SHA-1 per piece of the content."""
    pieces = info.get(b"pieces")
    piece_length = _piece_length(info)
    total_size = sum(file.size for file in files)
    if not isinstance(pieces, bytes) or len(pieces) % 20 or (total_size > 0 and not pieces):
        raise ValueError("torrent pieces field is invalid")
    if piece_length is None or piece_length <= 0:
        raise ValueError("torrent piece length is invalid")
    expected_pieces = 0 if total_size == 0 else (total_size + piece_length - 1) // piece_length
    if len(pieces) // 20 != expected_pieces:
        raise ValueError("torrent v1 piece count does not match content size")


def _v2_piece_length(info: dict[bytes, Any]) -> int:
    """BEP 52 piece length (pure v2 and hybrid): a power of two, at least 16 KiB."""
    piece_length = _piece_length(info)
    if piece_length is None or piece_length < 16 * 1024 or piece_length & (piece_length - 1):
        raise ValueError("torrent piece length is invalid")
    return piece_length


def _validate_v2_piece_layers(root: dict[bytes, Any], file_tree: Any, piece_length: int) -> None:
    """Every file larger than a piece has a piece layer that hashes to its pieces root,
    and no piece layer is left unused."""
    piece_layers = root.get(b"piece layers", {})
    malformed = not isinstance(piece_layers, dict) or any(
        not isinstance(key, bytes) or len(key) != 32 or not isinstance(value, bytes) or len(value) % 32
        for key, value in piece_layers.items()
    )
    if malformed:
        raise ValueError("torrent piece layers are invalid")
    used_piece_roots: set[bytes] = set()

    def validate_v2_leaf(node: dict[bytes, Any]) -> None:
        for key, value in node.items():
            if key == b"":
                if not isinstance(value, dict):
                    raise ValueError("v2 torrent leaf is invalid")
                size = _non_negative_int(value.get(b"length"), what="file length")
                pieces_root = value.get(b"pieces root")
                if size and (not isinstance(pieces_root, bytes) or len(pieces_root) != 32):
                    raise ValueError("v2 non-empty file has no valid pieces root")
                if not size and pieces_root is not None:
                    raise ValueError("v2 empty file has an unexpected pieces root")
                if size > piece_length:
                    layer = piece_layers.get(pieces_root)
                    count = (size + piece_length - 1) // piece_length
                    if not isinstance(layer, bytes) or len(layer) != count * 32:
                        raise ValueError("v2 piece layer is missing or has an invalid length")
                    if _piece_layer_root(layer, count, piece_length) != pieces_root:
                        raise ValueError("v2 piece layer does not match its pieces root")
                    used_piece_roots.add(pieces_root)
            elif isinstance(value, dict):
                validate_v2_leaf(value)

    validate_v2_leaf(file_tree)
    if set(piece_layers) != used_piece_roots:
        raise ValueError("torrent has unused v2 piece layers")


def _validate_unique_paths(files: list[TorrentFile]) -> None:
    """No two real files may land on the same path on disk (case- and Windows-insensitive)."""
    seen: set[str] = set()
    for file in files:
        if file.is_pad:
            continue
        key = windows_path_key(file.path)
        if key in seen:
            raise ValueError("torrent contains duplicate or case-colliding paths")
        seen.add(key)
    if not any(not file.is_pad for file in files):
        raise ValueError("torrent contains only padding files")


def parse_torrent_metadata(torrent: bytes) -> TorrentMetadata:
    root, info, start, end = _raw_info_span(torrent)
    raw_name = info.get(b"name.utf-8") or info.get(b"name")
    name = _validate_component(raw_name) if isinstance(raw_name, bytes) else ""
    if not name:
        raise ValueError("torrent name is missing")
    meta_version = info.get(b"meta version")
    if meta_version is not None and meta_version != 2:
        raise ValueError("unsupported torrent meta version")
    has_v1 = b"pieces" in info or b"files" in info or b"length" in info
    raw_info = torrent[start:end]
    hash_v1: str | None = None
    hash_v2: str | None = None
    v1_files: list[TorrentFile] | None = None
    if meta_version == 2:
        if has_v1 and b"pieces" not in info:
            raise ValueError("hybrid torrent has incomplete v1 metadata")
        files, is_multi = _v2_files(info)
        hash_v2 = hashlib.sha256(raw_info).hexdigest().upper()
        if has_v1 and b"pieces" in info:
            v1_files, _v1_multi = _v1_files(info, name)
            v1_signature = [(row.path, row.size) for row in v1_files if not row.is_pad]
            v2_signature = [(row.path, row.size) for row in files if not row.is_pad]
            if v1_signature != v2_signature:
                raise ValueError("hybrid torrent v1/v2 file trees disagree")
            hash_v1 = hashlib.sha1(raw_info).hexdigest().upper()
    else:
        if b"pieces" not in info:
            raise ValueError("v1 torrent has no pieces")
        files, is_multi = _v1_files(info, name)
        v1_files = files
        hash_v1 = hashlib.sha1(raw_info).hexdigest().upper()
    if b"pieces" in info:
        _validate_v1_pieces(info, v1_files or files)
    if meta_version == 2:
        piece_length = _v2_piece_length(info)
        _validate_v2_piece_layers(root, info[b"file tree"], piece_length)
        if hash_v1 and v1_files is not None:
            _validate_hybrid_alignment(v1_files, files, piece_length)
    _validate_unique_paths(files)
    return TorrentMetadata(
        name=name,
        infohash=hash_v2 or hash_v1 or "",
        client_hash=(hash_v2[:40] if hash_v2 else hash_v1 or ""),
        hash_v1=hash_v1,
        hash_v2=hash_v2,
        files=tuple(files),
        is_multi=is_multi,
        meta_version=meta_version,
    )


def display_name(torrent: bytes) -> str:
    try:
        return parse_torrent_metadata(torrent).name
    except ValueError:
        return ""


def infohash_v1(torrent: bytes) -> str:
    """Return the BEP 3 SHA-1 info hash; fail for a pure v2 torrent."""
    value = parse_torrent_metadata(torrent).hash_v1
    if value is None:
        raise ValueError("pure v2 torrent has no v1 info hash")
    return value


def infohash_v2(torrent: bytes) -> str:
    """Return the BEP 52 SHA-256 info hash; fail for a v1-only torrent."""
    value = parse_torrent_metadata(torrent).hash_v2
    if value is None:
        raise ValueError("v1 torrent has no v2 info hash")
    return value


def torrent_files(torrent: bytes) -> tuple[TorrentFile, ...]:
    return parse_torrent_metadata(torrent).files


def looks_like_torrent(data: bytes) -> bool:
    """Structural check only: a bencoded dict with an ``info`` dict.

    Policy violations (unsafe paths, collisions, bad v2 layers) must surface later
    from ``parse_torrent_metadata`` as a torrent error, not be mistaken for an
    HTML login page ("нужен вход").
    """
    try:
        decoded, end = _Decoder(data).parse()
    except TypeError, ValueError, RecursionError:
        return False
    if end != len(data) or not isinstance(decoded, dict):
        return False
    info = decoded.get(b"info")
    return (
        isinstance(info, dict)
        and isinstance(info.get(b"name"), bytes)
        and isinstance(info.get(b"piece length"), int)
        and (isinstance(info.get(b"pieces"), bytes) or isinstance(info.get(b"file tree"), dict))
    )


def is_download_limit(data: bytes) -> bool:
    raw = data[:12000]
    for enc in ("cp1251", "utf-8", "latin-1"):
        try:
            t = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        low = t.lower()
        if "торрент-файл" in low and ("сутки" in low or "скачали сегодня" in low or "недоступен" in low):
            return True
        if "количество торрент" in low:
            return True
    return False
