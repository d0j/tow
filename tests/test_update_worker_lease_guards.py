"""The checks before the web update's worker runs `<lease>/locks.py`, the one file TOW executes
from its data folder: each refusal leaves the job record as it was and never runs the module."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tow import update_worker

# A lock module that leaves a mark when it runs.
LOCKS = """\
from pathlib import Path

Path(__file__).with_name("executed").write_text("yes", encoding="utf-8")


def lock(handle, *, wait):
    return True


def unlock(handle):
    pass
"""
JOB_ID = "c" * 32


def _lease(tmp_path: Path) -> tuple[Path, dict, Path]:
    path = tmp_path / "job.json"
    job = {"id": JOB_ID, "status": "queued", "lease_version": 1}
    folder = tmp_path / JOB_ID
    folder.mkdir()
    (folder / "locks.py").write_text(LOCKS, encoding="utf-8")
    (folder / "worker.lock").write_bytes(b"\0")
    path.write_text(json.dumps(job), encoding="utf-8")
    return path, job, folder


def _redirect(target: Path, link: Path) -> None:
    """``link`` leads to the folder ``target``: a junction on Windows (no privilege needed), a
    symbolic link elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")


def _refused(tmp_path: Path, path: Path, folder: Path) -> None:
    before = path.read_bytes()
    assert update_worker.run(tmp_path, path, JOB_ID, "1.22.21", None) == 2
    assert path.read_bytes() == before  # an unsafe lease is no ownership, not even of a refusal
    assert not (folder / "executed").exists()


def test_a_sound_lease_runs_its_lock_module(tmp_path):
    """The control: the mark the refusals look for appears when the module runs."""
    path, job, folder = _lease(tmp_path)
    with update_worker.worker_lease(path, job) as acquired:
        assert acquired
    assert (folder / "executed").read_text(encoding="utf-8") == "yes"


@pytest.mark.parametrize("job_id", ["C" * 32, "c" * 31, "../" + "c" * 29])
def test_a_lease_identifier_that_is_not_a_plain_hex_name_is_refused(tmp_path, job_id):
    path, job, folder = _lease(tmp_path)
    refused = pytest.raises(ValueError, match="invalid worker lease identifier")
    with refused, update_worker.worker_lease(path, {**job, "id": job_id}):
        pytest.fail("an identifier outside [a-f0-9]{32} names no lease folder")
    assert not (folder / "executed").exists()


def test_a_lease_folder_that_is_a_file_is_refused(tmp_path):
    path, _job, folder = _lease(tmp_path)
    for member in folder.iterdir():
        member.unlink()
    folder.rmdir()
    folder.write_bytes(b"not a folder")
    before = path.read_bytes()
    assert update_worker.run(tmp_path, path, JOB_ID, "1.22.21", None) == 2
    assert path.read_bytes() == before


def test_a_lease_folder_that_leads_elsewhere_is_refused(tmp_path):
    path, _job, folder = _lease(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    folder.rename(elsewhere)
    _redirect(elsewhere, folder)
    _refused(tmp_path, path, elsewhere)


@pytest.mark.parametrize("member", ["worker.lock", "locks.py"])
def test_a_lease_member_that_is_a_folder_is_refused(tmp_path, member):
    path, _job, folder = _lease(tmp_path)
    (folder / member).unlink()
    (folder / member).mkdir()
    _refused(tmp_path, path, folder)


@pytest.mark.parametrize("member", ["worker.lock", "locks.py"])
def test_a_lease_member_that_leads_elsewhere_is_refused(tmp_path, member):
    path, _job, folder = _lease(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (folder / member).replace(elsewhere / member)
    _redirect(elsewhere, folder / member)
    _refused(tmp_path, path, folder)


def test_a_lock_module_python_cannot_load_is_refused(tmp_path, monkeypatch):
    path, job, folder = _lease(tmp_path)
    monkeypatch.setattr(update_worker.importlib.util, "spec_from_file_location", lambda *_args: None)
    with pytest.raises(ImportError, match="worker lease module unavailable"), update_worker.worker_lease(path, job):
        pytest.fail("no module, no lease")
    _refused(tmp_path, path, folder)


@pytest.mark.parametrize("status", ["", "running", "OK", None, 1])
def test_a_job_record_with_an_unknown_status_is_refused(tmp_path, status):
    path, job, folder = _lease(tmp_path)
    path.write_text(json.dumps({**job, "status": status}), encoding="utf-8")
    _refused(tmp_path, path, folder)


def test_a_job_record_that_is_a_folder_or_leads_elsewhere_is_refused(tmp_path):
    path, _job, folder = _lease(tmp_path)
    path.unlink()
    path.mkdir()
    assert update_worker.run(tmp_path, path, JOB_ID, "1.22.21", None) == 2
    assert path.is_dir()
    assert not any(path.iterdir())
    assert not (folder / "executed").exists()
    path.rmdir()
    elsewhere = tmp_path / "records"
    elsewhere.mkdir()
    _redirect(elsewhere, path)
    assert update_worker.run(tmp_path, path, JOB_ID, "1.22.21", None) == 2
    assert not any(elsewhere.iterdir())
    assert not (folder / "executed").exists()
