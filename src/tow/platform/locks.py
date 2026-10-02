"""A lock between processes: byte 0 of an open lock file (msvcrt on Windows, flock elsewhere).

The lock file must already hold its byte (``tow.store.init_lock_file``). These calls are the
real system's: unlike ``current()`` a test cannot swap them for another system's.
"""

from __future__ import annotations

import errno
import sys
import time
from typing import BinaryIO

_BUSY = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def lock(handle: BinaryIO, *, wait: bool = True, poll: float = 0.05) -> bool:
    """Take the lock; with ``wait=False`` return False at once when another process holds it."""
    if sys.platform == "win32":
        import msvcrt

        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if exc.errno not in _BUSY:
                    raise
                if not wait:
                    return False
                time.sleep(poll)
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def unlock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
