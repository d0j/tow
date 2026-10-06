"""Update smoke test: the previous release, installed as a user installs it, is updated by the real
updater to a source archive of this commit - to a broken copy that must roll back, and for real.

    python scripts/update-smoke.py --previous <folder> --source tow-source.tar.gz [--port 18879]

<folder> holds the previous release's SHA256SUMS and, on Windows, its TOW-windows-x64.zip
(unpacked, then "Start TOW.cmd"), elsewhere its install.sh and tow-source.tar.gz (install.sh
--dir, then the start file); every file is checked against that SHA256SUMS. The updates run the
command `tow update --ref <tag>` prints, with the updater's local options --source and --sums:

1. the installed (previous) updater installs a broken copy of this archive: the copy changes
   data/ and cannot start. The update must end rolled_back, with the previous code, data/ and
   version back and answering;
2. the installed updater installs this archive: it answers as its version, app/ holds exactly
   the archive's files, app.prev the previous code; data/, keys/master.key and config.yaml stay;
3. this archive's own updater installs the broken copy: rolled back to this archive;
4. the updater copy it left in runtime/ (what "Update TOW.cmd" runs after a cut-off switch)
   installs this archive again.

TOW is stopped at the end. The install is a new folder under RUNNER_TEMP (else the temp folder),
removed unless --keep. Standard library only (the workflows run it with
`uv run --no-project python`); never port 8787 (a real TOW may run there).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

WINDOWS = os.name == "nt"
SOURCE_NAME = "tow-source.tar.gz"
ZIP_NAME = "TOW-windows-x64.zip"
SENTINEL = "update-smoke.txt"
ADDED = "update-smoke-added.txt"
BEFORE = "written before the updates\n"
BROKEN_MARK = "update-smoke: broken on purpose"
# Appended to src/tow/__init__.py of the broken copy: every `tow` process of it changes data/ and
# exits, so the update's health check fails after the switch and the rollback must undo both.
BROKEN_CODE = f"""

# {BROKEN_MARK}: this copy changes data/ and cannot start; the updater must roll it back.
import os as _smoke_os
from pathlib import Path as _SmokePath

if _smoke_os.environ.get("TOW_ROOT"):
    _smoke_data = _SmokePath(_smoke_os.environ["TOW_ROOT"]) / "data"
    (_smoke_data / "{SENTINEL}").write_text("changed by the broken copy\\n", encoding="utf-8")
    (_smoke_data / "{ADDED}").write_text("added by the broken copy\\n", encoding="utf-8")
raise SystemExit("{BROKEN_MARK}")
"""
SKIPPED_PARTS = frozenset({".venv", "__pycache__"})
# What `tow update --ref <tag>` prints first: "<base python>" "<app>/scripts/update.py" --ref <tag>
COMMAND_RE = re.compile(r'^\s*"([^"]+)"\s+"([^"]+update\.py)"\s+--ref\s', re.MULTILINE)


class SmokeError(RuntimeError):
    pass


def say(text: str) -> None:
    print(f"update-smoke: {text}", flush=True)


def expect(condition: object, text: str) -> None:
    if not condition:
        raise SmokeError(text)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` lines by file name (as update.py and the installers read them)."""
    sums = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
        if match:
            sums[match.group(2)] = match.group(1).lower()
    return sums


def check_sums(folder: Path, names: list[str]) -> None:
    sums = parse_sums((folder / "SHA256SUMS").read_text(encoding="utf-8"))
    for name in names:
        expect(name in sums, f"the previous release's SHA256SUMS lists no {name}")
        expect(sha256(folder / name) == sums[name], f"{name} does not match the previous release's SHA256SUMS")


def write_sums(archive: Path) -> Path:
    sums = archive.with_name(archive.name + ".SHA256SUMS")
    sums.write_text(f"{sha256(archive)}  {SOURCE_NAME}\n", encoding="ascii")
    return sums


def _relative(name: str) -> str:
    """A member's path below the archive's one top folder."""
    return name.split("/", 1)[1] if "/" in name else ""


def archive_files(archive: Path) -> dict[str, str]:
    """Every file of a source archive (below its top folder) with its SHA-256."""
    files = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.isfile() and _relative(member.name):
                source = tar.extractfile(member)
                expect(source is not None, f"{archive.name}: unreadable {member.name}")
                assert source is not None
                files[_relative(member.name)] = hashlib.sha256(source.read()).hexdigest()
    return files


def archive_version(archive: Path) -> str:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.isfile() and _relative(member.name) == "pyproject.toml":
                source = tar.extractfile(member)
                assert source is not None
                match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', source.read().decode("utf-8"))
                if match:
                    return match.group(1)
    raise SmokeError(f"{archive.name} has no version in pyproject.toml")


def make_broken(source: Path, out: Path) -> Path:
    """A copy of the source archive whose src/tow/__init__.py changes data/ and exits."""
    patched = 0
    with tarfile.open(source, "r:gz") as tar, tarfile.open(out, "w:gz") as copy:
        for member in tar:
            data = None
            if member.isfile():
                extracted = tar.extractfile(member)
                assert extracted is not None
                data = extracted.read()
                if _relative(member.name) == "src/tow/__init__.py":
                    data += BROKEN_CODE.encode("utf-8")
                    member.size = len(data)
                    patched += 1
            copy.addfile(member, io.BytesIO(data) if data is not None else None)
    expect(patched == 1, f"{source.name} has no src/tow/__init__.py to break")
    return out


def tree_files(folder: Path) -> dict[str, str]:
    """Every file of an app folder with its SHA-256, without its environment and bytecode."""
    files = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if path.is_file() and not SKIPPED_PARTS.intersection(relative.parts):
            files[relative.as_posix()] = sha256(path)
    return files


def project_version(app: Path) -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (app / "pyproject.toml").read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def clean_environment() -> dict[str, str]:
    """TOW runs as on a new computer: no TOW, uv or virtual environment variables of this one."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("TOW_", "UV_"))
        and name.upper() not in {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"}
    }
    env["TOW_NO_BROWSER"] = "1"
    return env


class Smoke:
    def __init__(self, previous: Path, source: Path, port: int, base: Path):
        self.previous = previous
        self.source = source
        self.port = port
        self.folder = base / f"tow update smoke {os.getpid()}"
        self.work = self.folder / "archives"
        self.root = self.folder / "TOW"
        self.env = clean_environment()
        self.opener = build_opener(ProxyHandler({}))

    # --- processes -----------------------------------------------------------------------------

    def run(self, argv: list[str], *, env: dict[str, str] | None = None, capture: bool = False) -> tuple[int, str]:
        """Output goes to this log as it comes (a started TOW must not hold a pipe of ours)."""
        say("$ " + " ".join(f'"{part}"' if " " in part else part for part in argv))
        if WINDOWS and argv[0].lower().endswith(".cmd"):
            argv = ["cmd.exe", "/d", "/c", "call", *argv]
        done = subprocess.run(
            argv,
            env=env or self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1200,
            check=False,
        )
        if capture:
            print(done.stdout, flush=True)
        return done.returncode, done.stdout or ""

    def tow(self, *args: str, capture: bool = False) -> tuple[int, str]:
        launcher = self.root / "app" / "scripts" / ("tow.cmd" if WINDOWS else "tow")
        return self.run([str(launcher), *args], capture=capture)

    # --- the service ---------------------------------------------------------------------------

    def health(self) -> dict[str, object] | None:
        try:
            with self.opener.open(f"http://127.0.0.1:{self.port}/healthz", timeout=3) as response:
                value = json.loads(response.read().decode("utf-8"))
        except OSError, ValueError:
            return None
        return value if isinstance(value, dict) and value.get("ok") is True else None

    def wait(self, answering: bool, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if (self.health() is not None) == answering:
                return True
            time.sleep(1)
        return (self.health() is not None) == answering

    def version(self) -> str:
        health = self.health()
        return str(health.get("version")) if health else ""

    # --- the install ---------------------------------------------------------------------------

    def install_previous(self) -> str:
        if WINDOWS:
            check_sums(self.previous, [ZIP_NAME])
            say(f"unpacking the previous release's {ZIP_NAME} into {self.folder}")
            with zipfile.ZipFile(self.previous / ZIP_NAME) as bundle:
                bundle.extractall(self.folder)
            config = self.root / "config.yaml"
            text = re.sub(r"(?m)^port:.*$", f"port: {self.port}", config.read_text(encoding="utf-8"))
            config.write_text(text, encoding="utf-8")
            start = self.root / "Start TOW.cmd"
        else:
            check_sums(self.previous, ["install.sh", SOURCE_NAME])
            env = dict(self.env)
            env["TOW_INSTALL_SOURCE"] = str(self.previous / SOURCE_NAME)
            env["TOW_INSTALL_SUMS"] = str(self.previous / "SHA256SUMS")
            installer = str(self.previous / "install.sh")
            code, _ = self.run(["sh", installer, "--dir", str(self.root), "--port", str(self.port)], env=env)
            expect(code == 0, f"the previous release's install.sh exited with {code}")
            start = self.root / ("Start TOW.command" if sys.platform == "darwin" else "start-tow")
        code, _ = self.run([str(start)])
        expect(code == 0, f"{start.name} exited with {code}")
        expect(self.wait(True, 120), f"the previous release does not answer on {self.port}")
        version = self.version()
        say(f"the previous release answers as {version}")
        return version

    def update_command(self, ref: str) -> list[str]:
        """The updater command `tow update` prints (the documented way to update)."""
        code, output = self.tow("update", "--ref", ref, capture=True)
        match = COMMAND_RE.search(output)
        expect(code == 0 and match, "`tow update --ref` printed no updater command")
        assert match is not None
        return [match.group(1), match.group(2)]

    def update(self, command: list[str], ref: str, archive: Path, *, health_timeout: int) -> int:
        say(f"update with {command[1]} to {archive.name}")
        code, _ = self.run(
            [
                *command,
                "--ref",
                ref,
                "--source",
                str(archive),
                "--sums",
                str(write_sums(archive)),
                "--health-timeout",
                str(health_timeout),
                "--wait-minutes",
                "3",
            ]
        )
        return code

    def state(self) -> dict[str, object]:
        try:
            value = json.loads((self.root / "update-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SmokeError(f"update-state.json cannot be read: {exc}") from exc
        expect(isinstance(value, dict), "update-state.json is not an object")
        return value

    # --- the checks ----------------------------------------------------------------------------

    def kept(self) -> dict[str, str]:
        """What no update may change: the data sentinel, the key and the settings."""
        return {
            "data sentinel": (self.root / "data" / SENTINEL).read_text(encoding="utf-8"),
            "keys/master.key": sha256(self.root / "keys" / "master.key"),
            "config.yaml": sha256(self.root / "config.yaml"),
        }

    def check_kept(self, kept: dict[str, str]) -> None:
        for name, value in self.kept().items():
            expect(value == kept[name], f"{name} changed")
        expect(not (self.root / "data" / ADDED).exists(), f"data/{ADDED} of the broken copy is still there")

    def check_rolled_back(self, code: int, version: str, app: dict[str, str], kept: dict[str, str]) -> None:
        state = self.state()
        expect(code == 1, f"the broken update exited with {code}, not 1")
        expect(state.get("status") == "rolled_back", f"update-state.json says {state.get('status')}, not rolled_back")
        expect(state.get("data_restored") is True, "the broken copy changed data/, the rollback did not put it back")
        expect(self.wait(True, 30) and self.version() == version, f"TOW {version} does not answer after the rollback")
        expect(project_version(self.root / "app") == version, "app/ does not hold the previous version")
        expect(tree_files(self.root / "app") == app, "app/ differs from the code before the update")
        for leftover in ("app.new", ".update-switch.json", ".update-download"):
            expect(not (self.root / leftover).exists(), f"the rollback left {leftover}")
        self.check_kept(kept)
        say(f"rolled back to {version}: code, data and settings as before")

    def check_updated(self, code: int, version: str, target: dict[str, str], kept: dict[str, str]) -> None:
        state = self.state()
        expect(code == 0, f"the update exited with {code}" + (" (refused before it began)" if code == 2 else ""))
        expect(state.get("status") == "ok", f"update-state.json says {state.get('status')}, not ok")
        expect(self.version() == version, f"TOW {version} does not answer after the update")
        expect(tree_files(self.root / "app") == target, "app/ does not hold exactly the target archive")
        expect((self.root / "app.prev" / "pyproject.toml").is_file(), "the previous code is not kept in app.prev")
        self.check_kept(kept)
        say(f"updated to {version}: data and settings kept")

    # --- the run -------------------------------------------------------------------------------

    def run_all(self) -> None:
        expect(not self.folder.exists(), f"{self.folder} already exists")
        expect(self.health() is None, f"something already answers on port {self.port}")
        self.work.mkdir(parents=True)
        target_version = archive_version(self.source)
        ref = f"v{target_version}"
        good = self.work / SOURCE_NAME
        shutil.copyfile(self.source, good)
        broken = make_broken(good, self.work / "broken.tar.gz")
        target = archive_files(good)
        expect("scripts/update.py" in target, f"{self.source.name} has no scripts/update.py")

        previous_version = self.install_previous()
        expect(not (self.root / "app" / ".git").exists(), "the previous release is a git clone, not an archive install")
        (self.root / "data" / SENTINEL).write_text(BEFORE, encoding="utf-8")
        kept = self.kept()
        previous_app = tree_files(self.root / "app")

        say(f"1. the installed {previous_version} updater installs a broken {ref}")
        installed = self.update_command(ref)
        code = self.update(installed, ref, broken, health_timeout=60)
        self.check_rolled_back(code, previous_version, previous_app, kept)

        say(f"2. the installed {previous_version} updater installs {ref}")
        code = self.update(installed, ref, good, health_timeout=180)
        self.check_updated(code, target_version, target, kept)

        say(f"3. the updater of {ref} installs a broken {ref}")
        own = self.update_command(ref)
        expect(sha256(Path(own[1])) == target["scripts/update.py"], "`tow update` names another updater")
        code = self.update(own, ref, broken, health_timeout=60)
        self.check_rolled_back(code, target_version, target, kept)

        say(f"4. the updater copy in runtime/ installs {ref}")
        copy = self.root / "runtime" / "update.py"
        expect(copy.is_file() and sha256(copy) == target["scripts/update.py"], "runtime/update.py is not this updater")
        code = self.update([own[0], str(copy)], ref, good, health_timeout=180)
        self.check_updated(code, target_version, target, kept)

        code, _ = self.tow("stop")
        expect(code == 0 and self.wait(False, 90), "TOW still answers after `tow stop`")
        say("passed")

    def stop_and_report(self, failed: bool) -> None:
        if (self.root / "app" / "scripts").is_dir() and self.health() is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self.tow("stop")
                self.wait(False, 90)
        if failed:
            for log in ("update-state.json", "data/logs/run.log"):
                path = self.root / log
                if path.is_file():
                    say(f"--- {log} (end)")
                    print("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]), flush=True)

    def remove(self) -> None:
        for _attempt in range(10):
            shutil.rmtree(self.folder, ignore_errors=True)
            if not self.folder.exists():
                return
            time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--previous", type=Path, required=True, help="folder with the previous release's files")
    parser.add_argument("--source", type=Path, required=True, help=f"the source archive to update to ({SOURCE_NAME})")
    parser.add_argument("--port", type=int, default=18879, help="the test install's port (never 8787)")
    parser.add_argument("--keep", action="store_true", help="keep the test install's folder")
    args = parser.parse_args(argv)
    if args.port == 8787:
        parser.error("never on 8787 (a real TOW may run there)")
    base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    smoke = Smoke(args.previous.resolve(), args.source.resolve(), args.port, base)
    failed = True
    try:
        smoke.run_all()
        failed = False
    except (SmokeError, OSError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        say(f"FAILED: {exc}")
    finally:
        smoke.stop_and_report(failed)
        if not args.keep:
            smoke.remove()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
