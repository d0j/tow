"""Administrative torrent state, kept apart from a client's displayed health state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def matches_hash(info: Mapping[str, Any] | None, infohash: str) -> bool:
    """The requested identity, including either identity of a hybrid torrent."""
    wanted = str(infohash or "").strip().upper()
    identities = {str((info or {}).get(key) or "").strip().upper() for key in ("hash", "infohash_v1", "infohash_v2")}
    v2 = str((info or {}).get("infohash_v2") or "").strip().upper()
    if len(v2) == 64:
        identities.add(v2[:40])  # only an explicitly identified v2 hash has this short client id
    return bool(wanted) and wanted in identities


def is_stopped(info: Mapping[str, Any] | None) -> bool:
    """A stopped/paused task, or an errored task independently confirmed stopped.

    qBittorrent displays missing files and errors ahead of its independent stopped flag.
    Only the adapter's fresh, identity-bound evidence can resolve those ambiguous states;
    a flag must never turn an unknown or visibly running task into a stopped one.
    """
    state = str((info or {}).get("state") or "").casefold()
    if state in {"error", "missingfiles"}:
        return (info or {}).get("stopped") is True
    return state.startswith(("stopped", "paused"))
