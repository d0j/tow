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
