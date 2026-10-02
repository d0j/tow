"""Lock files: a brand-new lock file raced by two processes must not raise."""

import io

from tow.store import init_lock_file


class _LockedRegionFile(io.BytesIO):
    """Simulates Windows: the other process already wrote and locked byte 0."""

    def write(self, data):
        raise PermissionError(13, "region locked by another process")


def test_init_lock_file_tolerates_the_other_process_winning():
    handle = _LockedRegionFile()
    init_lock_file(handle)  # must not raise
    assert handle.tell() == 0


def test_init_lock_file_writes_the_lock_byte_once():
    handle = io.BytesIO()
    init_lock_file(handle)
    init_lock_file(handle)
    assert handle.getvalue() == b"\0"
    assert handle.tell() == 0
