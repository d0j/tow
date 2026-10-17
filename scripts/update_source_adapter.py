"""A loopback release source for testing a CANDIDATE's web update (TC-02), injected by the test harness.

TOW itself never imports this file and has no switch that reads it: release discovery
(``tow.releases``: GitHub's API through the public-only transport, no proxies, no redirects) and the
updater's downloads (``scripts/update.py``: GitHub and its download hosts over HTTPS) stay as they are,
and tests/test_update_source.py keeps them so. A smoke test (scripts/web-update-smoke.py) uses this
module on a throwaway install only:

1. ``serve(folder, tag)`` answers on 127.0.0.1 as GitHub answers a release: ``/releases/latest``, the
   tag's archive, the release's ``tow-source.tar.gz`` and ``SHA256SUMS`` (files of ``folder``).
2. ``install(site_packages, origin, tag)`` puts a ``.pth`` file and a module into the throwaway
   install's environment. In every TOW process of that environment they replace, at the two
   boundaries only, where the candidate looks: its release discovery answers ``tag`` (``patch_releases``)
   and the updater it copies for the web update's worker downloads from ``origin`` (``adapt_updater``).
   Everything between - the Settings request, the restore point, the job record, the broker or
   detached handoff, the worker's lease, the updater's checks, switch, health check, rollback and
   the job's read-back - is the candidate's own code.

This does not test an unmodified historical web update: a released version asks GitHub and copies
its own updater (that is the update smoke of a previous release, scripts/update-smoke.py). The next
``uv sync`` of the update builds a new environment, without these files. Standard library only.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO = "d0j/tow"
MODULE = "tow_test_update_source"
MARKER = "# --- test harness only (scripts/update_source_adapter.py)"
# Appended to the copy of update.py the web update's worker runs: its System downloads from the
# test origin (redirects stay within it: update.allowed_redirect allows the same origin only).
UPDATER_SUFFIX = """

# --- test harness only (scripts/update_source_adapter.py): downloads from {origin} ---
_HarnessSystem = System


class System(_HarnessSystem):  # noqa: F811
    def __init__(self, app, uv=None, *, github={origin!r}, repo={repo!r}):
        super().__init__(app, uv, github=github, repo=repo)

    def _opener(self, *handlers):
        return build_opener(ProxyHandler({{}}), *handlers)  # nothing between the worker and 127.0.0.1
"""


def loopback_origin(origin: str) -> str:
    """``origin`` when it is exactly http://127.0.0.1:<port> (no user, path, query or fragment)."""
    parts = urlsplit(origin)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"not a loopback origin: {origin!r}") from exc
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.netloc != f"127.0.0.1:{port}"
        or not port
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"not a loopback origin: {origin!r}")
    return origin


MAIN_GUARD = '\n\nif __name__ == "__main__":\n'


def adapt_updater(text: str, origin: str) -> str:
    """The updater's text with the test origin as its source (``UPDATER_SUFFIX``): before its
    ``if __name__ == "__main__":`` block, so a run of the file itself (the terminal update and the
    entry files a user double-clicks, which copy it to their own process) uses it too, not only
    the web update's worker, which imports it."""
    if MARKER in text:
        return text  # adapted already
    suffix = UPDATER_SUFFIX.format(origin=loopback_origin(origin), repo=REPO)
    at = text.rfind(MAIN_GUARD)
    if at < 0:
        return text + suffix
    return text[:at] + suffix + text[at:]


def patch_releases(releases: Any, version: str) -> None:
    """Release discovery answers ``version`` (the candidate's tag) and nothing else."""

    def published_version(asked: str) -> str:
        found = releases.version_parts(asked)
        if found is None or asked.removeprefix("v") != version:
            raise ValueError("not the test release")
        return version

    releases.published_version = published_version
    releases._fetch_latest = lambda: version


def patch_handoff(backend_class: type, origin: str) -> None:
    """The web update's copy of update.py, adapted right before its worker starts (the copy sits
    beside worker.py in the job folder)."""
    original: Callable[..., Any] = backend_class.spawn_detached

    def spawn_detached(self: Any, argv: Any, **options: Any) -> Any:
        words = [str(word) for word in argv]
        if "--handoff" in words:
            worker = next(Path(word) for word in words if word.endswith("worker.py"))
            copy = worker.with_name("update.py")
            copy.write_text(adapt_updater(copy.read_text(encoding="utf-8"), origin), encoding="utf-8")
        return original(self, argv, **options)

    backend_class.spawn_detached = spawn_detached


_MODULE_TEXT = '''"""Test harness only (scripts/update_source_adapter.py), in a throwaway install: delete to disable."""
import importlib.util as _util
import sys as _sys

_spec = _util.spec_from_file_location("_tow_update_source_adapter", {adapter!r})
_adapter = _util.module_from_spec(_spec)
_spec.loader.exec_module(_adapter)
try:
    import tow.platform as _platform
    import tow.releases as _releases
except ImportError:  # not TOW's environment any more
    pass
else:
    _adapter.patch_releases(_releases, {version!r})
    _adapter.patch_handoff(type(_platform.current()), {origin!r})
'''


def install(site_packages: Path, origin: str, tag: str) -> list[Path]:
    """The .pth and the module into ``site_packages`` of a throwaway install's environment; the
    files written (the harness removes them, and the update's new environment has none)."""
    version = tag.removeprefix("v")
    module = site_packages / f"{MODULE}.py"
    module.write_text(
        _MODULE_TEXT.format(adapter=str(Path(__file__).resolve()), version=version, origin=loopback_origin(origin)),
        encoding="utf-8",
    )
    pth = site_packages / f"{MODULE}.pth"
    pth.write_text(f"import {MODULE}\n", encoding="utf-8")
    return [module, pth]


class _Release(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes, dict[str, str]]] = {}
    asked: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        self.asked.append(self.path)
        status, body, headers = self.routes.get(self.path, (404, b"Not Found", {}))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


def routes(folder: Path, tag: str) -> dict[str, tuple[int, bytes, dict[str, str]]]:
    """GitHub's answers for a release whose assets are in ``folder``."""
    archive = (folder / "tow-source.tar.gz").read_bytes()
    sums = (folder / "SHA256SUMS").read_bytes()
    return {
        f"/{REPO}/releases/latest": (302, b"", {"Location": f"/{REPO}/releases/tag/{tag}"}),
        f"/{REPO}/archive/refs/tags/{tag}.tar.gz": (200, archive, {}),
        f"/{REPO}/releases/download/{tag}/tow-source.tar.gz": (200, archive, {}),
        f"/{REPO}/releases/download/{tag}/SHA256SUMS": (200, sums, {}),
    }


def serve(folder: Path, tag: str) -> tuple[str, ThreadingHTTPServer, list[str]]:
    """A loopback server of the release (origin, server, the paths asked); ``server.shutdown()``."""
    handler = type("Release", (_Release,), {"routes": routes(folder, tag), "asked": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server, handler.asked


if __name__ == "__main__":
    print(json.dumps({"module": MODULE, "marker": MARKER}))
