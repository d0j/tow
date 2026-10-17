"""Two network stacks on one runner (D01): TOW as a device on the network sees it.

    python scripts/netns-smoke.py --source tow-source.tar.gz [--port 18968]

Linux only, with `sudo -n` and `ip` (a disposable CI runner). Two network namespaces, A and B,
are joined by a veth pair (A 10.231.17.1/24, B 10.231.17.2/24). The candidate is installed with
this checkout's install/install.sh into a throwaway folder with a Cyrillic name, set to listen on
every address of its namespace (bind 0.0.0.0, network access on, a password of this run) and
started inside A. An HTTP client runs in each namespace as this account: in A on 127.0.0.1 (this
computer), in B at A's address from B's own address (a device on the network). These are
separate network stacks on one runner - their own interfaces, addresses, routes and sockets -
not a second device: the packets never leave the runner's kernel.

What it asserts: the server classifies B's peer as the network - pages go to the sign-in page,
JSON is refused, a wrong password is refused, each sign-in is its own session (signing one out
leaves the other), this-computer-only actions are refused - while A's 127.0.0.1 needs no
password; and an error that names private paths and a link's keys reaches B scrubbed: no path
and no key on any surface, while this computer is shown every word of it (Home's rows are, by
design) - so the planted error is known to be there.

Only the namespaces, the veth pair and processes of the install are touched; everything is
removed at the end (also after a failure), unless --keep. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ADDRESS_A, ADDRESS_B, PREFIX = "10.231.17.1", "10.231.17.2", 24
# Every page and JSON a signed-in device reads; an answer of another status is recorded too.
SURFACES = (
    "/",
    "/health.json",
    "/sync.json",
    "/messages.json",
    "/history",
    "/history?group=errors",
    "/log.json",
    "/settings",
)
# The planted error: paths and a link's keys - this computer may see them, the network never.
PATH_MARKERS = ("e01.mkv", "/home/owner-netns")
KEY_MARKERS = ("SECRETUK", "SECRETPK")
PLANTED_ERROR = (
    "qBittorrent: cannot write /srv/media/Secret Show/e01.mkv (/home/owner-netns/.config/TOW/state.json) "
    "https://tracker.example/download.php?id=1&uk=SECRETUK&pk=SECRETPK"
)

# Run by the install's Python with TOW_ROOT: the synthetic topics of scripts/screenshots.py, then
# that error on one of them as a scheduled check records it (the state, then a log event).
PLANT = """
import importlib.util, os
path = os.path.join(os.environ["TOW_ROOT"], "app", "scripts", "screenshots.py")
spec = importlib.util.spec_from_file_location("screens", path)
screens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screens)
screens.seed("en")
from tow.log import append_event, error_fields
from tow.storage.locks import persistence_lock
from tow.store import load_state, save_state
error = os.environ["NETNS_SMOKE_ERROR"]
fields = dict(error_fields(error))
with persistence_lock():
    state = load_state()
    topic = state["topics"][1]
    topic.update(last_ok=False, last_changed=False, last_error=error, last_error_class=fields.get("cls"))
    save_state(state)
append_event("check_fail", topic=topic["id"], title=topic["title"], url=topic["url"], how="auto", **fields)
"""


class SmokeError(RuntimeError):
    pass


def say(text: str) -> None:
    print(f"netns-smoke: {text}", flush=True)


def expect(condition: object, text: str) -> None:
    if not condition:
        raise SmokeError(text)


def _crash_smoke():
    spec = importlib.util.spec_from_file_location("crash_smoke", Path(__file__).with_name("crash-smoke.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the client, run inside a namespace ------------------------------------------------------------


def request(
    base: str, method: str, path: str, *, cookie: str = "", accept: str = "text/html", form: str | None = None
) -> dict[str, Any]:
    """One request without redirects: status, Location, the session cookie it sets, the body."""
    host = base.split("://", 1)[1]
    headers = {"Host": host, "Accept": accept, "Accept-Language": "en"}
    body = None
    if cookie:
        headers["Cookie"] = f"tow_session={cookie}"
    if method == "POST":
        headers["Origin"] = base
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = (form or "").encode("ascii")
        headers["Content-Length"] = str(len(body))
    name, _, port = host.rpartition(":")
    connection = http.client.HTTPConnection(name, int(port), timeout=20)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8", "replace")
        session = ""
        for header, value in response.getheaders():
            found = re.match(r"tow_session=([^;]*)", value) if header.lower() == "set-cookie" else None
            if found:
                session = found.group(1)
        return {
            "status": response.status,
            "location": response.getheader("Location") or "",
            "session": session,
            "text": text,
        }
    finally:
        connection.close()


def surfaces(base: str, cookie: str = "") -> dict[str, dict[str, Any]]:
    """Each surface's status and which planted markers it shows."""
    seen = {}
    for path in SURFACES:
        accept = "application/json" if ".json" in path else "text/html"
        answer = request(base, "GET", path, cookie=cookie, accept=accept)
        markers = [marker for marker in (*PATH_MARKERS, *KEY_MARKERS) if marker in answer["text"]]
        seen[path] = {"status": answer["status"], "markers": markers}
    return seen


def probe_network(base: str, password: str) -> dict[str, Any]:
    """What a device on the network meets: B's view of A."""
    found: dict[str, Any] = {}
    found["page"] = {k: v for k, v in request(base, "GET", "/").items() if k != "text"}
    found["json"] = {
        k: v for k, v in request(base, "GET", "/health.json", accept="application/json").items() if k != "text"
    }
    found["wrong"] = request(base, "POST", "/login", form="password=not-the-password")["status"]
    first = request(base, "POST", "/login", form="password=" + password)
    second = request(base, "POST", "/login", form="password=" + password)
    found["signin"] = [{k: v for k, v in one.items() if k != "text"} for one in (first, second)]
    found["sessions"] = [bool(first["session"]), bool(second["session"]), first["session"] != second["session"]]
    found["signed_in"] = request(base, "GET", "/", cookie=first["session"])["status"]
    found["surfaces"] = surfaces(base, first["session"])
    local_only = request(base, "POST", "/settings/access", cookie=first["session"], form="allow_lan=")
    found["local_only"] = {k: v for k, v in local_only.items() if k != "text"}
    request(base, "POST", "/logout", cookie=first["session"])
    found["after_signout"] = {
        "signed_out": {k: v for k, v in request(base, "GET", "/", cookie=first["session"]).items() if k != "text"},
        "other": request(base, "GET", "/", cookie=second["session"])["status"],
    }
    return found


def probe_local(base: str) -> dict[str, Any]:
    """This computer: A's own 127.0.0.1."""
    return {"page": request(base, "GET", "/")["status"], "surfaces": surfaces(base)}


def judge(network: dict[str, Any], local: dict[str, Any]) -> list[str]:
    """What is wrong with the two views (empty: the server told them apart as it should)."""
    problems = []
    page = network.get("page") or {}
    if page.get("status") != 303 or not str(page.get("location", "")).startswith("/login"):
        problems.append(f"a page from the network did not go to the sign-in page: {page}")
    if (network.get("json") or {}).get("status") != 401:
        problems.append(f"JSON from the network without a session was not refused with 401: {network.get('json')}")
    if network.get("wrong") != 401:
        problems.append(f"a wrong password was not refused with 401: {network.get('wrong')}")
    signin = network.get("signin") or []
    if [one.get("status") for one in signin] != [303, 303] or [one.get("location") for one in signin] != ["/", "/"]:
        problems.append(f"the password did not sign in: {signin}")
    if network.get("sessions") != [True, True, True]:
        problems.append(f"two sign-ins are not two sessions: {network.get('sessions')}")
    if network.get("signed_in") != 200:
        problems.append(f"a signed-in device does not get Home: {network.get('signed_in')}")
    local_only = network.get("local_only") or {}
    if local_only.get("status") not in (302, 303) or "/settings" not in str(local_only.get("location", "")):
        problems.append(f"turning network access off from the network was not refused: {local_only}")
    after = network.get("after_signout") or {}
    signed_out = after.get("signed_out") or {}
    if signed_out.get("status") != 303 or "/login" not in str(signed_out.get("location", "")):
        problems.append(f"a signed-out session still opens Home: {signed_out}")
    if after.get("other") != 200:
        problems.append(f"signing one device out signed the other out too: {after.get('other')}")
    leaks = {path: seen["markers"] for path, seen in (network.get("surfaces") or {}).items() if seen["markers"]}
    if leaks:
        problems.append(f"the network sees what it must not: {leaks}")
    if not network.get("surfaces") or all(seen["status"] != 200 for seen in network["surfaces"].values()):
        problems.append("no surface answered the signed-in device")
    if local.get("page") != 200:
        problems.append(f"this computer (127.0.0.1) did not get Home without a password: {local.get('page')}")
    shown = {marker for seen in (local.get("surfaces") or {}).values() for marker in seen["markers"]}
    if not shown & set(PATH_MARKERS):
        problems.append("this computer is not shown the planted error's paths: the error was not planted")
    return problems


# --- the namespaces and the install -------------------------------------------------------------------


def sudo(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(["sudo", "-n", *argv], capture_output=True, text=True, timeout=120, check=False)
    if check and done.returncode:
        raise SmokeError(f"sudo {' '.join(argv)} exited with {done.returncode}: {done.stderr.strip()}")
    return done


def minimal_env() -> dict[str, str]:
    """What a program in a namespace gets: this account's basics, no TOW_ or UV_ variable."""
    env = {name: os.environ[name] for name in ("HOME", "PATH", "USER", "LOGNAME") if name in os.environ}
    return {**env, "LANG": "C.UTF-8", "PYTHONUTF8": "1", "TOW_NO_BROWSER": "1"}


def as_me_in(namespace: str, argv: list[str], env: dict[str, str], *, ids: tuple[int, int] | None = None) -> list[str]:
    """``argv`` in ``namespace``, as this account (not root) with exactly ``env``."""
    uid, gid = ids or (os.getuid(), os.getgid())  # type: ignore[attr-defined,unused-ignore]
    return [
        "sudo", "-n", "ip", "netns", "exec", namespace,
        "setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups", "--",
        "env", "-i", *(f"{name}={value}" for name, value in env.items()), *argv,
    ]  # fmt: skip


class Stacks:
    """Namespaces A and B joined by a veth pair; removed again by ``remove``."""

    def __init__(self) -> None:
        tag = str(os.getpid())
        self.a, self.b = f"tow-a-{tag}", f"tow-b-{tag}"
        self.veth_a, self.veth_b = f"tva{tag}"[:15], f"tvb{tag}"[:15]

    def create(self) -> None:
        sudo("ip", "netns", "add", self.a)
        sudo("ip", "netns", "add", self.b)
        sudo("ip", "link", "add", self.veth_a, "type", "veth", "peer", "name", self.veth_b)
        for namespace, veth, address in ((self.a, self.veth_a, ADDRESS_A), (self.b, self.veth_b, ADDRESS_B)):
            sudo("ip", "link", "set", veth, "netns", namespace)
            sudo("ip", "-n", namespace, "addr", "add", f"{address}/{PREFIX}", "dev", veth)
            sudo("ip", "-n", namespace, "link", "set", veth, "up")
            sudo("ip", "-n", namespace, "link", "set", "lo", "up")
        for namespace in (self.a, self.b):
            say(
                f"{namespace}: "
                + " | ".join(sudo("ip", "-n", namespace, "-brief", "addr").stdout.split("\n")).strip(" |")
            )
        # Two stacks: B reaches A only over the pair, by address; A's loopback is A's own.
        ping = sudo("ip", "netns", "exec", self.b, "ping", "-c", "1", "-W", "5", ADDRESS_A, check=False)
        expect(ping.returncode == 0, f"{self.b} does not reach {ADDRESS_A} over the veth pair")

    def pids(self, namespace: str) -> list[int]:
        done = sudo("ip", "netns", "pids", namespace, check=False)
        return [int(pid) for pid in done.stdout.split() if pid.isdigit()]

    def remove(self) -> None:
        for namespace in (self.a, self.b):
            sudo("ip", "netns", "del", namespace, check=False)  # its end of the pair goes with it


class Smoke:
    def __init__(self, base: Path, port: int) -> None:
        crash = _crash_smoke()
        self.crash = crash
        self.install = crash.Install(base, port)
        self.install.folder = base / f"tow netns smoke Тест {os.getpid()}"
        self.install.root = self.install.folder / "TOW"
        self.port = port
        self.stacks = Stacks()
        self.password = secrets.token_urlsafe(18)

    @property
    def root(self) -> Path:
        return self.install.root

    def python(self, code: str, **env: str) -> str:
        """``code`` in the install's own Python, on the install (TOW stopped)."""
        environment = dict(self.install.env, TOW_ROOT=str(self.root), PYTHONUTF8="1", **env)
        done = subprocess.run(
            [str(self.root / "app" / ".venv" / "bin" / "python"), "-c", code],
            env=environment, capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        expect(done.returncode == 0, f"the install's Python failed: {done.stderr.strip()[-600:]}")
        return done.stdout

    def configure(self) -> None:
        """Listen on every address of its namespace, network access on, check no updates."""
        config = self.root / "config.yaml"
        text = config.read_text(encoding="utf-8")
        for key, value in (("bind", "0.0.0.0"), ("allow_lan", "true"), ("check_updates", "false")):
            line = f"{key}: {value}"
            text, count = re.subn(rf"(?m)^{key}:.*$", line, text)
            if not count:
                text = text.rstrip("\n") + f"\n{line}\n"
        config.write_text(text, encoding="utf-8")
        self.python(
            "import os; from tow import access; p = os.environ['NETNS_SMOKE_PASSWORD']; access.set_password(p, p)",
            NETNS_SMOKE_PASSWORD=self.password,
        )
        # Synthetic topics (scripts/screenshots.py's), then an error another process recorded.
        self.python(PLANT, NETNS_SMOKE_ERROR=PLANTED_ERROR)

    def in_a(self, *args: str, timeout: float = 300) -> tuple[int, str]:
        """A `tow` command of the install, run inside namespace A."""
        launcher = str(self.root / "app" / "scripts" / "tow")
        return self.install.run(as_me_in(self.stacks.a, [launcher, *args], minimal_env()), timeout=timeout)

    def probe(self, namespace: str, role: str, base: str) -> dict[str, Any]:
        secret = self.install.folder / "password.txt"
        secret.write_text(self.password, encoding="utf-8")
        secret.chmod(0o600)
        argv = [sys.executable, str(Path(__file__).resolve()), "probe", role, base, str(secret)]
        done = subprocess.run(
            as_me_in(namespace, argv, minimal_env()),
            capture_output=True, text=True, timeout=300, check=False,
        )  # fmt: skip
        expect(done.returncode == 0, f"the {role} client in {namespace} failed: {done.stderr.strip()[-800:]}")
        return json.loads(done.stdout)

    def run(self, source: Path) -> None:
        self.install.install(None, source)
        self.configure()
        self.stacks.create()
        code, _ = self.in_a("start")
        expect(code == 0, f"tow start in {self.stacks.a} exited with {code}")
        local = self.probe(self.stacks.a, "local", f"http://127.0.0.1:{self.port}")
        network = self.probe(self.stacks.b, "network", f"http://{ADDRESS_A}:{self.port}")
        print(json.dumps({"this computer (A, 127.0.0.1)": local, f"the network (B, {ADDRESS_B})": network}, indent=1))
        problems = judge(network, local)
        expect(not problems, "; ".join(problems))
        say(f"B ({ADDRESS_B}) is the network: sign-in required, sessions of their own, scrubbed texts")
        say("A's 127.0.0.1 is this computer: no password, the error's words shown")
        code, _ = self.in_a("stop")
        expect(code == 0, f"tow stop in {self.stacks.a} exited with {code}")

    def clean(self, *, keep: bool) -> None:
        if (self.root / "app" / "scripts").is_dir():
            with contextlib.suppress(Exception):
                self.in_a("stop", timeout=120)
        for pid in self.stacks.pids(self.stacks.a):
            if self.crash.ours(pid, self.root):  # only what runs from the install
                sudo("kill", "-9", str(pid), check=False)
        self.stacks.remove()
        if not keep:
            shutil.rmtree(self.install.folder, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["probe"]:
        role, base, secret = arguments[1:4]
        found = probe_local(base) if role == "local" else probe_network(base, Path(secret).read_text(encoding="utf-8"))
        print(json.dumps(found))
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True, help="the source archive (tow-source.tar.gz)")
    parser.add_argument("--port", type=int, default=18968, help="the test install's port (never 8787)")
    parser.add_argument("--keep", action="store_true", help="keep the test install's folder")
    args = parser.parse_args(arguments)
    if args.port == 8787:
        parser.error("never on 8787 (a real TOW may run there)")
    if not sys.platform.startswith("linux") or shutil.which("ip") is None or shutil.which("setpriv") is None:
        parser.error("network namespaces need Linux with ip and setpriv")
    smoke = Smoke(Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()), args.port)
    failed = True
    try:
        smoke.run(args.source.resolve())
        failed = False
        say("passed (separate network stacks on one runner, not a second device)")
    except (SmokeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        say(f"FAILED: {exc}")
        say("--- data/logs/run.log (end)")
        print("\n".join(smoke.install.run_log().splitlines()[-40:]), flush=True)
    finally:
        smoke.clean(keep=args.keep)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
