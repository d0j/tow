"""The README pictures: Home of a throwaway install with made-up topics, in English and Russian,
dark and light, 1200 px wide at scale 2.

    uv run --frozen --with playwright python scripts/screenshots.py [--port 18893] [--keep]

The install is a new folder in the temp folder (TOW_ROOT), with config.example.yaml, a fresh
master key and synthetic topics, history and health only - never a real install, never port
8787. `tow serve` runs on --port and is stopped at the end; the folder is removed unless --keep.
Writes docs/images/home-{en,ru}-{dark,light}.png. Playwright needs a Chromium it already has
(`playwright install chromium` downloads one); without it the script says so and exits 2.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
IMAGES = REPO / "docs" / "images"
WIDTH = 1200

# Made-up topics: one per state the Home page shows (new, done, fine, site unreachable, paused).
TOPICS: dict[str, list[tuple[str, str, str]]] = {
    # (title, link, folder)
    "en": [
        ("Northern Lights · season 2", "https://rutor.info/torrent/1234561", "D:/TV/Northern Lights"),
        ("The Long Road · season 1", "https://rutracker.org/forum/viewtopic.php?t=1234562", "D:/TV/The Long Road"),
        ("Clockwork Harbor", "https://kinozal.tv/details.php?id=1234563", "D:/Films"),
        ("Glass Garden · season 3", "https://nnmclub.to/forum/viewtopic.php?t=1234564", "D:/TV/Glass Garden"),
        ("Silent Orbit", "https://rutor.info/torrent/1234565", "D:/Films"),
    ],
    "ru": [
        ("Северное сияние · сезон 2", "https://rutor.info/torrent/1234561", "D:/Сериалы/Северное сияние"),
        ("Долгая дорога · сезон 1", "https://rutracker.org/forum/viewtopic.php?t=1234562", "D:/Сериалы/Долгая дорога"),
        ("Часовая гавань", "https://kinozal.tv/details.php?id=1234563", "D:/Фильмы"),
        ("Стеклянный сад · сезон 3", "https://nnmclub.to/forum/viewtopic.php?t=1234564", "D:/Сериалы/Стеклянный сад"),
        ("Тихая орбита", "https://rutor.info/torrent/1234565", "D:/Фильмы"),
    ],
}


def _iso(moment: datetime) -> str:
    """A fixed made-up offset: the pictures never show this computer's time zone."""
    return moment.astimezone(timezone(timedelta(hours=1))).isoformat(timespec="seconds")


def seed(language: str) -> None:
    """Write the synthetic topics, their history and a healthy client into the install."""
    from tow.store import save_download_history, save_state

    now = datetime.now(UTC)
    topics: list[dict[str, Any]] = []
    for number, (title, url, folder) in enumerate(TOPICS[language], 1):
        topics.append(
            {
                "id": f"topic-{number}",
                "title": title,
                "url": url,
                "save_path": folder,
                "hash": f"{number:040X}",
                "client_id": "default",
                "last_ok": True,
                "last_check": _iso(now - timedelta(minutes=10)),
            }
        )
    topics[0]["last_changed"] = True
    topics[3].update(
        last_ok=False,
        last_error="nnmclub: all mirrors are paused",
        last_error_code="mirrors.all_paused",
        last_error_params={"tracker": "nnmclub"},
        last_error_class="frozen",
    )
    topics[4]["paused"] = True
    save_state(
        {
            "topics": topics,
            "mirrors": {},
            "health": {"qbit_ok": True, "check_ok": True, "at_ts": int(time.time()) - 600},
        }
    )
    episode = {"kind": "episode_completed", "label": "S02E03", "at": _iso(now - timedelta(hours=2))}
    season = {"kind": "torrent_completed", "label": "S01E08", "at": _iso(now - timedelta(days=3))}
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "topic-1": {
                    "expected": {"kind": "episodes", "total": 10, "confidence": "high"},
                    "summary": {"completed": 3, "expected": 10, "is_complete": False},
                    "last_event": episode,
                    "items": {
                        f"episode:s02e0{n}": {
                            "label": f"S02E0{n}",
                            "status": "completed",
                            "completed_observed_at": episode["at"],
                        }
                        for n in (1, 2, 3)
                    },
                },
                "topic-2": {
                    "expected": {"kind": "episodes", "total": 8, "confidence": "high"},
                    "summary": {"completed": 8, "expected": 8, "is_complete": True},
                    "last_event": season,
                    "items": {
                        f"episode:s01e0{n}": {
                            "label": f"S01E0{n}",
                            "status": "completed",
                            "completed_observed_at": season["at"],
                        }
                        for n in range(1, 9)
                    },
                },
            },
        }
    )


def _seed_in(env: dict[str, str], language: str) -> None:
    """Seed in a child with the throwaway TOW_ROOT, so this process never opens TOW's files."""
    subprocess.run([sys.executable, __file__, "--seed", language], env=env, check=True)


def wait_for(url: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as answer:
                if answer.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"TOW did not answer at {url} within {seconds:.0f} s")


def _browsers() -> list[dict[str, Any]]:
    """Playwright's own Chromium, else one an earlier Playwright left, else Chrome or Edge."""
    found: list[dict[str, Any]] = [{}]
    home = Path.home()
    caches = [
        home / "AppData/Local/ms-playwright",
        home / ".cache/ms-playwright",
        home / "Library/Caches/ms-playwright",
    ]
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        caches.insert(0, Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    names = ("chrome-win64/chrome.exe", "chrome-win/chrome.exe", "chrome-linux/chrome", "chrome-linux64/chrome")
    for cache in caches:
        for folder in sorted(cache.glob("chromium-*"), reverse=True):
            found += [{"executable_path": str(folder / name)} for name in names if (folder / name).is_file()]
    found += [{"channel": "chrome"}, {"channel": "msedge"}]
    return found


def shoot(base: str, out: Path, env: dict[str, str]) -> list[Path]:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as missing:
        raise SystemExit(
            "Playwright is missing: uv run --frozen --with playwright python scripts/screenshots.py"
        ) from missing
    written = []
    with sync_playwright() as playwright:
        browser = None
        for options in _browsers():
            try:
                browser = playwright.chromium.launch(**options)
                break
            except Error:
                continue
        if browser is None:
            print("no Chromium for Playwright (playwright install chromium); no pictures taken", file=sys.stderr)
            sys.exit(2)
        for language, locale in (("en", "en-US"), ("ru", "ru-RU")):
            _seed_in(env, language)
            for scheme in ("dark", "light"):
                context = browser.new_context(
                    viewport={"width": WIDTH, "height": 800},
                    device_scale_factor=2,
                    locale=locale,
                    color_scheme=scheme,
                    timezone_id="Europe/Berlin",
                )
                page = context.new_page()
                page.goto(base + "/", wait_until="networkidle")
                page.add_style_tag(
                    content="*, *::before, *::after { transition: none !important; caret-color: transparent; }"
                )
                path = out / f"home-{language}-{scheme}.png"
                # The frame ends below the list: no empty page, no floating version badge.
                bottom = page.evaluate("Math.ceil(document.querySelector('main').getBoundingClientRect().bottom)")
                height = min(int(bottom) + 16, page.viewport_size["height"] if page.viewport_size else 800)
                page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": WIDTH, "height": height})
                written.append(path)
                context.close()
        browser.close()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18893)
    parser.add_argument("--out", type=Path, default=IMAGES)
    parser.add_argument("--keep", action="store_true", help="keep the throwaway install")
    parser.add_argument("--seed", choices=sorted(TOPICS), help=argparse.SUPPRESS)  # run inside TOW_ROOT
    args = parser.parse_args(argv)
    if args.seed:
        if "tow-screens-" not in os.environ.get("TOW_ROOT", ""):
            parser.error("--seed writes only into the throwaway install this script makes")
        seed(args.seed)
        return 0
    if args.port == 8787:
        parser.error("never port 8787: a real TOW may run there")

    root = Path(tempfile.mkdtemp(prefix="tow-screens-"))
    env = {key: value for key, value in os.environ.items() if not key.startswith("TOW_")}
    env.update(TOW_ROOT=str(root), PYTHONUTF8="1", TZ="CET-1")  # one made-up zone, not this computer's
    config = (REPO / "config.example.yaml").read_text(encoding="utf-8")
    (root / "config.yaml").write_text(config + "\nsetup_done: true\nlanguage: auto\n", encoding="utf-8")
    server = None
    log = None
    try:
        subprocess.run([sys.executable, "-m", "tow", "keys", "ensure"], env=env, check=True, capture_output=True)
        _seed_in(env, "en")
        log = (root / "serve.log").open("w", encoding="utf-8")
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tow",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
                "--parent-pid",
                str(os.getpid()),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{args.port}"
        wait_for(base + "/healthz")
        args.out.mkdir(parents=True, exist_ok=True)
        for path in shoot(base, args.out, env):
            print(path.relative_to(REPO) if path.is_relative_to(REPO) else path)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        if log is not None:
            log.close()
        if args.keep:
            print(f"kept {root}")
        else:
            # On Windows the venv's python.exe starts the real interpreter as its child, which may
            # hold the log a moment longer than the process we stopped.
            for _ in range(20):
                shutil.rmtree(root, ignore_errors=True)
                if not root.exists():
                    break
                time.sleep(0.5)
            else:
                print(f"could not remove {root}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
