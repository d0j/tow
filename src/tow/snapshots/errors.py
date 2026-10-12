"""The error every night-copy operation raises, and the reason of a failure as said to the owner."""

from __future__ import annotations


class SnapshotError(RuntimeError):
    pass


def reason(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__  # never a local path
    return str(exc) or type(exc).__name__
