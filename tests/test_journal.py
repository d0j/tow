"""``tow.journal``: a crash journal touches only what it wrote itself.

A journal folder holds its own files and the leftovers of an interrupted atomic write of one of
them (``.<own name>.<random>.tmp``). Anything else - another program's ``*.tmp`` included - makes
the folder unsafe: it is neither restored from nor deleted. A path that cannot even be looked at
is an error, never "no link here".
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tow.journal import Copy, Journal, digest, is_digest
from tow.store import atomic_write_bytes

MARKER = "MARKER.json"
OWN = frozenset({MARKER, "state.bin"})


class JournalError(RuntimeError):
    pass


def _journal(root: Path) -> Journal:
    return Journal(root, MARKER, OWN, JournalError, label="test journal", store="test store")


@pytest.fixture
def journal(tmp_path) -> Journal:
    root = tmp_path / ".journal"
    root.mkdir()
    return _journal(root)


def _publish(journal: Journal, target: Path, before: bytes) -> list[Copy]:
    """A journal that holds ``before`` as the copy of ``target``, its marker published."""
    (journal.root / "state.bin").write_bytes(before)
    (journal.root / MARKER).write_text("{}", encoding="utf-8")
    return [Copy("state", target, "state.bin", digest(before))]


@pytest.mark.parametrize(
    "name",
    [".state.bin.abcd1234.tmp", ".state.bin.k_9x2mzq.tmp", ".MARKER.json.0123abcd.tmp", ".state.bin.fixture.tmp"],
)
def test_a_leftover_of_its_own_atomic_write_is_the_journals_and_is_removed(journal, name):
    (journal.root / name).write_bytes(b"partial")

    journal.check_folder()
    journal.recover(lambda _marker: None)

    assert not journal.root.exists()


@pytest.mark.parametrize(
    "name",
    [
        "notes.tmp",
        "state.bin.tmp",
        ".state.bin.tmp",
        ".other.bin.abcd1234.tmp",
        ".state.bin.abcd1234.tmp.keep",
        ".state.bin.ab.cd.tmp",
        ".state.bin.ABCD1234.tmp",
        ".state.bin..tmp",
        "..state.bin.abcd1234.tmp",
    ],
)
def test_a_foreign_temporary_file_is_never_deleted_and_blocks_the_journal(journal, tmp_path, name):
    target = tmp_path / "state.json"
    target.write_bytes(b"after")
    copies = _publish(journal, target, b"before")
    foreign = journal.root / name
    foreign.write_bytes(b"someone else's")

    with pytest.raises(JournalError, match="unexpected entry"):
        journal.check_folder()
    with pytest.raises(JournalError, match="unexpected entry"):
        journal.recover(lambda _marker: copies)
    with pytest.raises(JournalError, match="unexpected entry"):
        journal.remove()

    assert foreign.read_bytes() == b"someone else's"
    assert target.read_bytes() == b"after"  # nothing restored from a folder that is not only the journal's
    assert (journal.root / MARKER).exists()


def test_the_temporaries_atomic_write_really_leaves_are_recognised(journal):
    with tempfile.NamedTemporaryFile(prefix=".state.bin.", suffix=".tmp", dir=journal.root, delete=False) as handle:
        name = Path(handle.name).name
    assert journal._own_temporary(name)
    atomic_write_bytes(journal.root / "state.bin", b"x")  # leaves nothing behind when it succeeds
    journal.check_folder()


def test_an_unpublished_journal_is_only_removed(journal, tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"after")
    (journal.root / "state.bin").write_bytes(b"before")

    assert journal.recover(lambda _marker: pytest.fail("an unpublished marker is never read")) is None

    assert not journal.root.exists()
    assert target.read_bytes() == b"after"


def test_a_published_journal_puts_the_stores_back_then_goes(journal, tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"after")
    copies = _publish(journal, target, b"before")

    assert journal.recover(lambda marker: copies if marker == {} else None) is None

    assert target.read_bytes() == b"before"
    assert not journal.root.exists()


def test_a_committed_journal_keeps_the_stores_and_goes(journal, tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"after")
    _publish(journal, target, b"before")

    journal.recover(lambda _marker: None)

    assert target.read_bytes() == b"after"
    assert not journal.root.exists()


def test_no_journal_folder_is_nothing_to_recover(tmp_path):
    journal = _journal(tmp_path / "absent")
    assert journal.exists() is False
    assert journal.recover(lambda _marker: pytest.fail("no marker to read")) is None


def test_a_copy_with_another_checksum_restores_nothing(journal, tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"after")
    [copy] = _publish(journal, target, b"before")

    with pytest.raises(JournalError, match="checksum mismatch"):
        journal.recover(lambda _marker: [Copy(copy.name, copy.target, copy.backup, digest(b"other"))])

    assert target.read_bytes() == b"after"
    assert journal.root.exists()


def test_a_path_that_cannot_be_inspected_is_an_error_not_a_plain_folder(journal, monkeypatch):
    real = Path.lstat

    def denied(self, *args, **kwargs):
        if self == journal.root:
            raise PermissionError(13, "Access is denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(JournalError, match="cannot inspect test journal path"):
        journal.is_link(journal.root)
    with pytest.raises(JournalError, match="cannot inspect"):
        journal.exists()
    with pytest.raises(JournalError, match="cannot inspect"):
        journal.recover(lambda _marker: None)
    assert journal.root.exists()


def test_a_missing_path_is_simply_not_a_link(journal):
    assert journal.is_link(journal.root / "absent") is False
    assert journal.is_link(journal.root) is False


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (digest(b"x"), True),
        (digest(b"x").upper(), False),
        (digest(b"x")[:63], False),
        (digest(b"x") + "0", False),
        ("g" * 64, False),
        ("0" * 63 + "g", False),
        (None, False),
        (b"0" * 64, False),
    ],
)
def test_a_checksum_is_64_lowercase_hex_digits(value, ok):
    assert is_digest(value) is ok
