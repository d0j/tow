"""The git hooks in .githooks: POSIX scripts git can run on every OS, and the post-rewrite hook
that runs the quick gate after an amend or a rebase without ever failing the rewrite."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = sorted((ROOT / ".githooks").iterdir())


@pytest.mark.parametrize("hook", HOOKS, ids=[hook.name for hook in HOOKS])
def test_every_hook_is_a_posix_script_with_unix_line_ends(hook):
    data = hook.read_bytes()
    assert data.startswith(b"#!/bin/sh\n")
    assert b"\r" not in data


@pytest.mark.allow_system  # a read-only query of this checkout's index
def test_git_records_every_hook_as_executable():
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    done = subprocess.run(
        [git, "-C", str(ROOT), "ls-files", "-s", ".githooks"], capture_output=True, text=True, check=False
    )
    modes = {line.rsplit("\t", 1)[1]: line.split(" ", 1)[0] for line in done.stdout.splitlines()}
    if not modes:
        pytest.skip("the hooks are not committed in this checkout yet")
    assert {name: mode for name, mode in modes.items() if mode != "100755"} == {}


def _fake_pwsh(folder: Path, status: int) -> None:
    """A ``pwsh`` that records its arguments and ends with ``status``."""
    script = folder / "pwsh"
    script.write_text(f'#!/bin/sh\necho "$@" > "{folder.as_posix()}/args"\nexit {status}\n', encoding="utf-8")
    script.chmod(0o755)


@pytest.mark.allow_system  # sh with a fake pwsh, in the test's folder
@pytest.mark.parametrize("status", [0, 1])
def test_post_rewrite_runs_the_quick_gate_and_never_fails_the_rewrite(tmp_path, status):
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX shell")
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    _fake_pwsh(bin_folder, status)
    environment = {**os.environ, "PATH": f"{bin_folder}{os.pathsep}{os.environ.get('PATH', '')}"}

    done = subprocess.run(
        [sh, str(ROOT / ".githooks" / "post-rewrite"), "rebase"],
        cwd=tmp_path,
        env=environment,
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert (bin_folder / "args").read_text(encoding="utf-8").split() == [
        "-NoProfile",
        "-File",
        "scripts/gate.ps1",
        "-Quick",
    ]
    assert "git rebase rewrote commits" in done.stderr
    assert ("THE QUICK GATE FAILED AFTER git rebase" in done.stderr) is (status != 0)
