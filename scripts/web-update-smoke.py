"""Web update smoke of a CANDIDATE (TC-02): Settings' update, the real handoff, the job's read-back.

    python scripts/web-update-smoke.py --zip TOW-windows-x64.zip --source tow-source.tar.gz [--port 18958]

The candidate's bundle is unpacked into a throwaway folder (a Cyrillic name) and started offline on
--port (never 8787). Its source archive is served as the release v1.99.0 from 127.0.0.1 by
scripts/update_source_adapter.py, which the harness injects into that install's environment at the
two boundaries only: release discovery and the updater's download source. Then, as a browser of
this computer does, it POSTs /updates/install and follows /updates/status:

1. a release whose SHA256SUMS does not match: the job ends refused (or failed), TOW answers as the
   candidate, the job record reads back the same and no staging folder is left;
2. the release itself: the job ends ok, /healthz answers 1.99.0, the job record says ok.

This tests the candidate's web handoff, not an unmodified historical web update (a released
version asks GitHub; scripts/update-smoke.py updates a previous release). Windows bundle only for
now. Standard library only; TOW is stopped and the folder removed at the end unless --keep.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

TAG = "v1.99.0"
VERSION = TAG[1:]
TERMINAL = {"ok", "refused", "failed", "rolled_back", "recovered", "interrupted", "superseded"}


def say(text: str) -> None:
    print(f"web-update-smoke: {text}", flush=True)


def _adapter():
    spec = importlib.util.spec_from_file_location(
        "update_source_adapter", Path(__file__).with_name("update_source_adapter.py")
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release(source: Path, folder: Path, *, good: bool) -> None:
    """The candidate's archive as the release v1.99.0 (its top folder and version), and SHA256SUMS."""
    folder.mkdir(parents=True)
    out = io.BytesIO()
    with tarfile.open(source) as src, tarfile.open(fileobj=out, mode="w:gz") as dst:
        for member in src:
            name = member.name.split("/", 1)
            member.name = f"tow-{VERSION}" + (f"/{name[1]}" if len(name) > 1 else "")
            # A pax "path" (a long or non-ASCII name read back) wins over member.name when written.
            member.pax_headers.pop("path", None)
            data = src.extractfile(member).read() if member.isfile() else None
            if data is not None and member.name == f"tow-{VERSION}/pyproject.toml":
                text = data.decode("utf-8")
                start = text.index('version = "') + len('version = "')
                data = (text[:start] + VERSION + text[text.index('"', start) :]).encode("utf-8")
            if data is not None:
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
    archive = out.getvalue()
    (folder / "tow-source.tar.gz").write_bytes(archive)
    digest = hashlib.sha256(archive if good else archive + b"!").hexdigest()
    (folder / "SHA256SUMS").write_text(f"{digest}  tow-source.tar.gz\n", encoding="utf-8")


class Install:
    def __init__(self, base: Path, port: int):
        self.root = base / "TOW"
        self.app = self.root / "app"
        self.port = port
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("TOW_", "UV_"))}
        self.env.update(UV_OFFLINE="1", TOW_NO_BROWSER="1")
        self.http = build_opener(ProxyHandler({}))

    def tow(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["cmd.exe", "/d", "/c", str(self.app / "scripts" / "tow.cmd"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env, cwd=self.root,
            timeout=timeout, check=False,
        )  # fmt: skip

    def get(self, path: str) -> dict | None:
        try:
            with self.http.open(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
                return json.loads(response.read())
        except OSError, ValueError:
            return None

    def post(self, path: str, form: dict[str, str]) -> tuple[int, dict]:
        origin = f"http://127.0.0.1:{self.port}"
        request = Request(origin + path, data=urlencode(form).encode(), headers={"Origin": origin}, method="POST")
        try:
            with self.http.open(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except OSError as exc:
            body = getattr(exc, "read", lambda: b"{}")()
            return getattr(exc, "code", 0), json.loads(body or b"{}")

    def wait(self, condition, seconds: float, what: str) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(1)
        raise SystemExit(f"web-update-smoke: FAILED: {what} not within {seconds:.0f} s")

    def start(self) -> None:
        done = self.tow("start", timeout=180)
        self.wait(lambda: (self.get("/healthz") or {}).get("ok") is True, 120, f"TOW answering ({done.stdout[-300:]})")

    def stop(self) -> None:
        self.tow("stop", timeout=180)
        self.wait(lambda: self.get("/healthz") is None, 120, "TOW stopping")

    def job(self) -> dict:
        return json.loads((self.root / "runtime" / "web-update" / "job.json").read_text(encoding="utf-8"))


def leg(install: Install, adapter, folder: Path, *, good: bool) -> dict:
    origin, server, asked = adapter.serve(folder, TAG)
    try:
        site = install.app / ".venv" / "Lib" / "site-packages"
        files = adapter.install(site, origin, TAG)
        install.stop()
        install.start()  # every TOW process of the environment now has the test source
        code, answer = install.post("/updates/install", {"version": VERSION})
        say(f"POST /updates/install: {code} {answer}")
        if code != 202 or answer.get("status") != "queued":
            raise SystemExit(f"web-update-smoke: FAILED: the update was not queued: {code} {answer}")
        status: dict = {}

        def finished() -> bool:
            nonlocal status
            status = install.get("/updates/status") or status
            return status.get("status") in TERMINAL

        install.wait(finished, 900, "the job's end")
        job = install.job()
        say(f"/updates/status: {status.get('status')} {status.get('error') or ''}; job.json: {job.get('status')}")
        if not good:
            for path in files:
                path.unlink(missing_ok=True)
        return {"status": status, "job": job, "asked": list(asked), "health": install.get("/healthz")}
    finally:
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18958)
    parser.add_argument("--base", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("web-update-smoke: Windows bundle only for now")
    if args.port == 8787:
        raise SystemExit("web-update-smoke: never port 8787 (a real TOW may run there)")
    base = Path(tempfile.mkdtemp(prefix="web-update Тест ", dir=args.base))
    adapter = _adapter()
    install = Install(base, args.port)
    if install.get("/healthz") is not None:
        raise SystemExit(f"web-update-smoke: port {args.port} is in use")
    try:
        with zipfile.ZipFile(args.zip) as archive:
            archive.extractall(base)
        config = install.root / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8").replace("port: 8787", f"port: {args.port}"), "utf-8")
        done = install.tow("setup")
        if done.returncode != 0:
            raise SystemExit(f"web-update-smoke: FAILED: setup: {done.stdout[-500:]}{done.stderr[-500:]}")
        before = None
        install.start()
        before = (install.get("/healthz") or {}).get("version")
        say(f"the candidate answers as {before} on {args.port}")
        release(args.source, base / "bad", good=False)
        release(args.source, base / "good", good=True)

        bad = leg(install, adapter, base / "bad", good=False)
        if (
            bad["status"].get("status") not in {"refused", "failed"}
            or bad["job"].get("status") != bad["status"]["status"]
        ):
            raise SystemExit(f"web-update-smoke: FAILED: the bad release: {bad['status']} / {bad['job']}")
        if (bad["health"] or {}).get("version") != before:
            raise SystemExit(f"web-update-smoke: FAILED: after the bad release TOW answers {bad['health']}")
        left = [name for name in ("app.new", ".update-download", "app.failed") if (install.root / name).exists()]
        if left:
            raise SystemExit(f"web-update-smoke: FAILED: staging left: {left}")
        say("1. a release that does not match its SHA256SUMS: refused, the candidate answers, nothing left")

        good = leg(install, adapter, base / "good", good=True)
        if good["status"].get("status") != "ok" or good["job"].get("status") != "ok":
            raise SystemExit(f"web-update-smoke: FAILED: the release: {good['status']} / {good['job']}")
        install.wait(lambda: (install.get("/healthz") or {}).get("version") == VERSION, 60, f"/healthz {VERSION}")
        if good["job"].get("target") != VERSION or f"/d0j/tow/releases/download/{TAG}/SHA256SUMS" not in good["asked"]:
            raise SystemExit(f"web-update-smoke: FAILED: the job or the source: {good['job']} {good['asked']}")
        say(f"2. the release: the job ended ok, /healthz answers {VERSION}, job.json says ok")
        say("passed")
        return 0
    finally:
        with contextlib.suppress(Exception):
            install.stop()
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            say(f"kept: {base}")


if __name__ == "__main__":
    sys.exit(main())
