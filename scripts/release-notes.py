"""Release notes for a tag: what to download (English, Russian), then the tag's changelog sections.

Usage: python3 scripts/release-notes.py v1.22.1 d0j/tow > notes.md (the release workflow runs it).
Standard library only.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section(changelog: Path, version: str) -> str:
    """The body of `## [version]` in a Keep a Changelog file ("" when absent)."""
    lines, found = [], False
    for line in changelog.read_text(encoding="utf-8").splitlines():
        if line.startswith("## ["):
            if found:
                break
            found = line.startswith(f"## [{version}]")
            continue
        if found:
            lines.append(line)
    return "\n".join(lines).strip()


def notes(tag: str, repo: str) -> str:
    version = tag.removeprefix("v")
    latest = f"https://github.com/{repo}/releases/latest/download"
    guide = f"https://github.com/{repo}/blob/{tag}/docs/install.md"
    guide_ru = f"https://github.com/{repo}/blob/{tag}/docs/ru/install.md"
    english = section(ROOT / "CHANGELOG.md", version) or f"TOW {version}"
    russian = section(ROOT / "CHANGELOG.ru.md", version)
    parts = [
        "## Download",
        "",
        "| Your system | What to do |",
        "|---|---|",
        (
            "| **Windows** | Download **TOW-windows-x64.zip** below, extract it anywhere, "
            "double-click **Start TOW.cmd**. Everything is inside; no internet needed for the first start. |"
        ),
        f"| Windows, one line | In PowerShell: `irm {latest}/install.ps1 \\| iex` |",
        (
            f"| **macOS, Linux** | In Terminal: `curl -LsSf {latest}/install.sh \\| sh` — then start TOW with "
            "`~/TOW/Start TOW.command` (macOS, double-click) or `~/TOW/start-tow` (Linux). |"
        ),
        "",
        (
            "Files below: **install.ps1** and **install.sh** are what the one-line commands run; "
            "**tow-source.tar.gz** is the source that install.sh and updates download; **SHA256SUMS** lists the "
            "checksums the installers check; *Source code* is GitHub's own archive, for developers."
        ),
        f"Step by step: [install guide]({guide}) · After installing, back up `TOW/keys/master.key`.",
        "",
        "## What's new",
        "",
        english,
    ]
    if russian:
        parts += [
            "",
            "<details>",
            "<summary><b>По-русски</b></summary>",
            "",
            "## Что скачать",
            "",
            "| Система | Что сделать |",
            "|---|---|",
            (
                "| **Windows** | Скачайте **TOW-windows-x64.zip** ниже, распакуйте куда угодно, дважды щёлкните "
                "**Start TOW.cmd**. Всё внутри; для первого запуска интернет не нужен. |"
            ),
            f"| Windows, одной строкой | В PowerShell: `irm {latest}/install.ps1 \\| iex` |",
            (
                f"| **macOS, Linux** | В Терминале: `curl -LsSf {latest}/install.sh \\| sh` — затем запускайте "
                "`~/TOW/Start TOW.command` (macOS, двойной щелчок) или `~/TOW/start-tow` (Linux). |"
            ),
            "",
            (
                "Файлы ниже: **install.ps1** и **install.sh** запускают команды одной строкой; **tow-source.tar.gz** — "
                "исходный код, который скачивают install.sh и обновления; **SHA256SUMS** — контрольные суммы, которые "
                "проверяют установщики; *Source code* — архив самого GitHub, для разработчиков."
            ),
            f"Пошагово: [инструкция]({guide_ru}) · После установки сохраните копию `TOW/keys/master.key`.",
            "",
            "## Что нового",
            "",
            russian,
            "",
            "</details>",
        ]
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: release-notes.py <tag> <owner/repo>")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(notes(sys.argv[1], sys.argv[2]))
