"""The B36 record of real clients' spellings (tests/integration/client_names.py), without a client.

real.yml records how qBittorrent and Transmission spell the names Windows reserves and checks
TOW's keys against that record; here the cases build into torrents with the right info hash, the
exact spellings are written and listed as the file system keeps them, and the verdict tells an
agreeing record from one that would extend (or contradict) TOW's device set.
"""

from __future__ import annotations

import os

import pytest
from integration.client_names import (
    CASES,
    folder_of,
    mismatches,
    on_disk,
    remove_tree,
    torrent_of,
    write_spelling,
)

from tow.torrent import parse_torrent_metadata


def test_each_case_is_its_own_torrent_with_its_info_hash():
    hashes = set()
    for index in range(len(CASES)):
        content, infohash = torrent_of(index)
        hashes.add(infohash)
        try:
            metadata = parse_torrent_metadata(content)
        except ValueError:
            continue  # TOW refuses this spelling itself; the client is still asked
        assert metadata.client_hash.lower() == infohash
        assert [row.path for row in metadata.files] == ["/".join(CASES[index])]
    assert len(hashes) == len(CASES)
    names = set(CASES)
    for required in (("CON",), ("aux.txt",), ("COM0",), ("LPT0",), ("COM¹",), ("CONIN$",), ("trailing dot.",)):
        assert required in names
    assert ("trailing space ",) in names


def test_spellings_are_written_and_listed_exactly(tmp_path):
    index = CASES.index(("COM0", "inner.bin"))
    folder = tmp_path / "case"
    assert write_spelling(folder, index, ["COM0", "inner.bin"]) is None
    assert on_disk(folder) == [f"{folder_of(index)}/COM0/inner.bin"]
    with open(os.path.join(folder, folder_of(index), "COM0", "inner.bin"), "rb") as handle:
        assert len(handle.read()) == 1200


@pytest.mark.skipif(os.name != "nt", reason="the Windows spellings a plain path cannot name")
def test_windows_device_names_and_trailing_dots_are_kept_through_the_raw_path(tmp_path):
    for parts in (["CON"], ["trailing dot."], ["trailing space "]):
        index = CASES.index(tuple(parts))
        try:
            assert write_spelling(tmp_path / str(index), index, parts) is None
            assert on_disk(tmp_path / str(index)) == [f"{folder_of(index)}/{parts[0]}"]
        finally:
            remove_tree(tmp_path / str(index))  # a plain path cannot remove these names
        assert not (tmp_path / str(index)).exists()


def _record(**case):
    base = {"torrent": "tow-names-00/CON", "listed": "tow-names-00/CON", "created": []}
    return {"client": "qbittorrent", "os": "windows", "cases": [{**base, **case}]}


def test_a_record_that_agrees_with_tows_keys_has_no_mismatch():
    assert mismatches(_record()) == []
    assert mismatches(_record(listed="tow-names-00/CON_", created=["tow-names-00/con_"])) == []
    assert mismatches(_record(reads_torrent_spelling=False, reads_listed_spelling=True)) == []
    assert mismatches(_record(listed=None, add_error="refused")) == []  # nothing TOW could match


def test_a_client_rename_tows_keys_do_not_fold_is_reported():
    record = _record(torrent="tow-names-09/COM0", listed="tow-names-09/COM0_")
    [problem] = mismatches(record)
    assert "'tow-names-09/com0' != 'tow-names-09/com0_'" in problem
    assert problem.startswith("qbittorrent on windows: 'tow-names-09/COM0'")


def test_a_file_on_disk_under_another_spelling_than_listed_is_reported():
    [problem] = mismatches(_record(listed="tow-names-00/CON_", reads_torrent_spelling=True))
    assert "'tow-names-00/CON' on disk but listed as 'tow-names-00/CON_'" in problem
    linux = {**_record(created=["tow-names-00/con"]), "os": "linux"}
    assert len(mismatches(linux)) == 1  # a case-sensitive file system: another file
