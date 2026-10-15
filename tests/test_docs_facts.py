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


_ADDRESS = re.compile(
    r"(?<![\w.:])(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}(?:\.\d{1,3}){0,3})(?![\w.:])"
)
# Dotted text that is not an address: a fake Deluge's libtorrent version.
_NOT_ADDRESSES = frozenset({"2.1.1.0"})


def test_tests_use_only_the_named_public_addresses():
    """Tests used several real public addresses ad hoc; CONTRIBUTING asks for synthetic ones.

    A public address (as ``tow.net_guard.is_public`` judges it, an embedded IPv4 included) is one
    of the named constants of ``tests/helpers.py``, which CONTRIBUTING lists."""
    from helpers import PUBLIC_ADDRESSES

    from tow.net_guard import is_public

    found = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        for match in _ADDRESS.finditer(path.read_text(encoding="utf-8")):
            literal = match.group(1)
            try:
                public = is_public(literal)
            except ValueError:
                continue
            if public and literal not in _NOT_ADDRESSES and not any(name in literal for name in PUBLIC_ADDRESSES):
                found.append(f"{path.relative_to(ROOT)}: {literal}")
    assert found == [], "use a constant of tests/helpers.py (PUBLIC_IPV4...) or a documentation address"
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for name in ("PUBLIC_IPV4", "PUBLIC_IPV6", "tests/helpers.py"):
        assert f"`{name}`" in contributing, name


@pytest.mark.parametrize(
    ("guide", "words"),
    [
        ("docs/guide.md", ("**Needs attention**", "closed by default", "disappears", "focuses the fix button")),
        ("docs/ru/guide.md", ("**Требует внимания**", "закрыт по умолчанию", "исчезает", "ставит фокус на")),
    ],
)
def test_the_guides_explain_the_current_problem_triangle_and_recovery(guide, words):
    """Both guides describe the closed overlay, confirmed recovery and actionable links."""
    text = " ".join((ROOT / guide).read_text(encoding="utf-8").split())
    for word in words:
        assert word in text, word


@pytest.mark.parametrize(
    "doc",
    ["README.md", "README.ru.md", "docs/guide.md", "docs/ru/guide.md", "docs/install.md", "docs/ru/install.md"],
)
def test_text_lines_fit_120_columns(doc):
    """README.ru.md had a 124-character line; tables, links and HTML may be longer (repository style)."""
    long = [
        f"{doc}:{number}"
        for number, line in enumerate((ROOT / doc).read_text(encoding="utf-8").splitlines(), 1)
        if len(line) > 120 and not line.lstrip().startswith(("|", "<")) and "](" not in line and "://" not in line
    ]
    assert long == []


@pytest.mark.parametrize(
    ("doc", "words"),
    [
        ("README.md", ("[Site access](docs/PROTECTION.md) |", "**Site behind a browser check?**")),
        ("README.ru.md", ("[Доступ к сайтам](docs/ru/protection.md) |", "**Сайт за браузерной проверкой?**")),
        ("docs/guide.md", ("| **Settings** | **Site access**", "| Site retry |", "checks you start wait for it too")),
        (
            "docs/ru/guide.md",
            (
                "| **Настройки** | **Доступ к сайтам**",
                "| Повтор по сайту |",
                "проверки, запущенные вами, тоже его ждут",
            ),
        ),
    ],
)
def test_the_docs_name_site_access_and_site_retries(doc, words):
    """Settings starts with Site access and checks are retried after an outage since 1.30, but the README and the
    guides did not say so; the guide still said a manual check ignores a site's own request to wait (1.30.0)."""
    text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
    for word in words:
        assert word in text, word


def test_the_architecture_names_every_module():
    """docs/architecture.md lacked tow.web.check_jobs, tow.check_outcomes, tow.site_retries, the client
    verification, the on-demand site-access module and its broker, and many supporting modules.

    Every top-level module and package of ``tow``, and every module of the packages below (route modules
    excepted: "one ``APIRouter`` per ``routes_*.py``"), is named by its dotted name or, inside its package's
    entry, by its own name in backticks."""
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    source = ROOT / "src" / "tow"
    names = [
        f"tow.{path.stem}"
        for path in sorted(source.iterdir())
        if not path.name.startswith("_") and (path.suffix == ".py" or (path / "__init__.py").is_file())
    ]
    names += [
        f"tow.{package}.{path.stem}"
        for package in ("check", "protection", "supervisor", "platform", "web")
        for path in sorted((source / package).glob("*.py"))
        if path.stem != "__init__" and not path.stem.startswith("routes_")
    ]

    def named(name):
        return re.search(rf"`{re.escape(name)}[`.(]", text) or f"`{name.rsplit('.', 1)[-1]}`" in text

    assert [name for name in names if not named(name)] == []
    for fact in ("site_retry_attempts", "site_retry_batch", "--site-retry-only", "verification_pending"):
        assert fact in text, fact
    roadmap = " ".join((ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8").split())
    for fact in ("automatic retries after a site outage", "waits in a bounded queue", "long verification of existing"):
        assert fact in roadmap, fact


def test_contributing_describes_the_release_flow_in_use():
    """Releases are area commits plus a version-only release commit in one rebase-merged pull request, and the
    merged head branch is deleted; CONTRIBUTING spoke only of merging "the release commit"."""
    contributing = " ".join((ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").split())
    for words in (
        "holds the area commits",
        "changes nothing but the version",
        "**Rebase and merge**",
        "deletes the merged head branch",
    ):
        assert words in contributing, words
