"""The commit-msg hook: "area: what changed" (at most 72 characters), a blank line and a body;
merges, reverts, fixups and release commits keep their own form; one author, no trailers."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "commit-msg"
BODY = "\n\nWhy it changed and how it was verified.\n"
SCISSORS = "# ------------------------ >8 ------------------------\n"  # git commit -v


def _posix(path: Path) -> str:
    """The path as the shell sees it (Git for Windows' sh wants /c/... for C:\\...)."""
    text = path.as_posix()
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


def _run(tmp_path: Path, message: str) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_bytes(message.encode("utf-8"))
    return subprocess.run(
        [shell, _posix(HOOK), _posix(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        check=False,
    )


ACCEPTED = [
    "web: sign out of this device from the header" + BODY,
    "install.sh: run everything from main() called on the last line" + BODY,
    "restore points: decrypt only the points rotation removes" + BODY,
    "README: eight features and the update row" + BODY,
    "net guard: judge NAT64 addresses by their IPv4 part" + BODY,
    "a" * 60 + ": " + "b" * 10 + BODY,  # exactly 72
    "docs: Windows line endings\r\n\r\nThe body.\r\n",
    "\n\ndocs: leading blank lines are dropped" + BODY,
    "docs: comments are not part of it\n# Please enter the commit message\n\nThe body.\n",
    "docs: below the scissors\n\nThe body.\n" + SCISSORS + "Co-Authored-By: x\n",
    "release: v1.24.0 - updates recover from interruptions without losing any data\n",
    'Revert "web: sign out of this device from the header"\n\nThis reverts commit 41b3c77.\n',
    "fixup! web: sign out of this device\n",
    "squash! web: sign out of this device\n",
    "Merge pull request #75 from owner/docs/cleanup\n",
    "Merge branch 'main' into docs/cleanup\n",
    "",  # git refuses an empty message itself
]

REFUSED = {
    "no area": ("Add a sign-out button" + BODY, "area: what changed"),
    "capitalised area": ("Web: sign out" + BODY, "area: what changed"),
    "no space after the colon": ("web:sign out" + BODY, "area: what changed"),
    "space before the colon": ("web : sign out" + BODY, "area: what changed"),
    "empty text": ("web: " + BODY, "area: what changed"),
    "too long": ("web: " + "x" * 68 + BODY, "73 characters"),
    "no body": ("web: sign out of this device\n", "add a body"),
    "blank body": ("web: sign out of this device\n\n   \n\n", "add a body"),
    "no blank line": ("web: sign out of this device\nThe body.\n", "second line blank"),
    "co-author": ("web: sign out" + BODY + "\nCo-authored-by: Someone <x@example.com>\n", "one author"),
    "generated": ("web: sign out" + BODY + "\nGenerated with a tool\n", "one author"),
    "trailer in a merge": ("Merge branch 'x'\n\nCO-AUTHORED-BY: y\n", "one author"),
    "release without a summary": ("release: v1.24.0\n", "add a body"),
}


@pytest.mark.allow_system
@pytest.mark.parametrize("message", ACCEPTED)
def test_the_hook_accepts(tmp_path, message):
    done = _run(tmp_path, message)
    assert done.returncode == 0, done.stderr


@pytest.mark.allow_system
@pytest.mark.parametrize("name", sorted(REFUSED))
def test_the_hook_refuses(tmp_path, name):
    message, reason = REFUSED[name]
    done = _run(tmp_path, message)
    assert done.returncode == 1
    assert reason in done.stderr
    assert "COMMIT_EDITMSG" in done.stderr


def test_the_hook_is_a_posix_script_with_unix_line_ends():
    data = HOOK.read_bytes()
    assert data.startswith(b"#!/bin/sh\n")
    assert b"\r" not in data


@pytest.mark.allow_system  # a read-only query of this checkout's index
def test_git_records_the_hook_as_executable():
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    done = subprocess.run(
        [git, "-C", str(ROOT), "ls-files", "-s", ".githooks/commit-msg"],
        capture_output=True,
        text=True,
        check=False,
    )
    if not done.stdout.strip():
        pytest.skip("the hook is not committed in this checkout yet")
    assert done.stdout.startswith("100755 ")
