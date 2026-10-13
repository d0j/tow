"""What the README and the guides say about the program matches the program."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tow import i18n
from tow.trackers.presets import known_sites

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("readme", ["README.md", "README.ru.md"])
def test_the_readme_names_every_built_in_site_as_the_program_does(readme):
    """The table said "rutor" and "fast-torrent" where Sites shows "Rutor" and "Fast-Torrent"."""
    lines = (ROOT / readme).read_text(encoding="utf-8").splitlines()
    row = next(line for line in lines if line.startswith(("| **Sites**", "| **Сайты**")))
    named = {name.strip() for name in row.split("|")[2].split(";")[0].split(",")}
    assert named == {spec["title"] for spec in known_sites().values()}


@pytest.mark.parametrize(
    ("language", "guide", "pattern"),
    [("en", "docs/guide.md", r"in use by (.+?)(?: \(|\.)"), ("ru", "docs/ru/guide.md", r"занят (.+?)(?: \(|\.)")],
)
def test_the_guide_quotes_every_message_of_a_busy_port(language, guide, pattern):
    """The guide had one row, "often a second TOW", for three messages that name the holder."""
    text = (ROOT / guide).read_text(encoding="utf-8")
    for key in ("supervisor.port_busy", "supervisor.port_busy_tow", "supervisor.port_busy_own"):
        holder = re.search(pattern, i18n.translate(key, language)).group(1)
        assert holder in text, (key, holder)


def test_portable_names_what_the_installers_workflow_watches():
    """The installers workflow also runs when tow.storage changes; PORTABLE.md did not say so."""
    workflow = (ROOT / ".github" / "workflows" / "installers.yml").read_text(encoding="utf-8")
    watched = re.findall(r'^\s+- "src/tow/([^"]+)"', workflow, re.MULTILINE)
    portable = (ROOT / "docs" / "PORTABLE.md").read_text(encoding="utf-8")
    paragraph = " ".join(
        portable[portable.index("- Installers (`.github/workflows/installers.yml`)") :].split("\n- ")[0].split()
    )
    for path in watched:
        if path.startswith(("update", "web_update", "locales/")):  # "the updater", "the texts"
            continue
        assert f"`{path.removesuffix('**')}`" in paragraph, path


def test_contributing_names_every_golden_file():
    """CONTRIBUTING listed the golden texts without the Settings page."""
    contributing = " ".join((ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").split())
    said = {
        "cli-help.en.txt": "`tow --help` in both languages",
        "cli-help.ru.txt": "`tow --help` in both languages",
        "schedule-grid.txt": "the schedule on a fake clock",
        "settings-page.html": "the Settings page",
        "public_api.json": "`test_public_api.py` (public names and signatures)",
    }
    assert sorted(path.name for path in (ROOT / "tests" / "golden").iterdir()) == sorted(said)
    for name, words in said.items():
        assert words in contributing, name


@pytest.mark.parametrize(
    ("guide", "words"),
    [
        ("docs/guide.md", ("**Needs attention**", "folds", "opens again by itself", "puts the focus on the")),
        ("docs/ru/guide.md", ("**Требует внимания**", "сворачивает", "раскрывается сам", "ставит фокус на")),
    ],
)
def test_the_guides_say_how_the_needs_attention_banner_works(guide, words):
    """Qa10: the banner is new in this release, and neither guide said it folds, opens again on a
    new problem, or that its items lead to the fix."""
    text = " ".join((ROOT / guide).read_text(encoding="utf-8").split())
    for word in words:
        assert word in text, word
