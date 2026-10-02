"""Every relative link in the Markdown documentation points at a file that exists, and every
#anchor at a heading of that file (GitHub's heading ids)."""

import re
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted(
    [
        *ROOT.glob("*.md"),
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / ".github").rglob("*.md"),
    ]
)
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# The target of every link and image, also the outer link of a badge: [![alt](image)](target).
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _prose(text: str) -> str:
    """The text a reader sees as Markdown: no code blocks, inline code or comments."""
    return _INLINE_CODE.sub("", _COMMENT.sub("", _FENCE.sub("", text)))


def _slug(heading: str) -> str:
    """GitHub's id for a heading: link text kept, lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "")
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


@cache
def _anchors(path: Path) -> frozenset[str]:
    text = _COMMENT.sub("", _FENCE.sub("", path.read_text(encoding="utf-8")))
    seen: dict[str, int] = {}
    anchors = set()
    for heading in _HEADING.findall(text):
        slug = _slug(heading)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return frozenset(anchors)


def _links(path: Path) -> list[str]:
    return [
        target
        for target in _LINK.findall(_prose(path.read_text(encoding="utf-8")))
        if not re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE)  # http:, https:, mailto:
    ]


def test_there_are_documents_to_check():
    names = {path.relative_to(ROOT).as_posix() for path in DOCS}
    assert {"README.md", "README.ru.md", "docs/guide.md", "docs/ru/guide.md"} <= names


def _broken(doc: Path) -> list[str]:
    broken = []
    for target in _links(doc):
        file_part, _, anchor = target.partition("#")
        destination = (doc.parent / file_part).resolve() if file_part else doc
        if not destination.exists():
            broken.append(f"{target}: no such file")
        elif anchor and destination.suffix == ".md" and anchor not in _anchors(destination):
            broken.append(f"{target}: no heading #{anchor}")
    return broken


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_relative_links_resolve(doc):
    assert not _broken(doc)


def test_a_broken_link_or_anchor_is_found(tmp_path):
    (tmp_path / "other.md").write_text("# Other\n\n## Real part\n", encoding="utf-8")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "# Doc\n\n[ok](other.md#real-part) [self](#doc) [web](https://example.org/x)\n"
        "[gone](missing.md) [bad anchor](other.md#nope) `[code](skipped.md)`\n"
        "```text\n[fenced](skipped.md)\n```\n",
        encoding="utf-8",
    )
    assert _broken(doc) == ["missing.md: no such file", "other.md#nope: no heading #nope"]


def test_slugs_follow_github():
    assert _slug("2. One process: `tow run` (supervisor)") == "2-one-process-tow-run-supervisor"
    assert _slug("Code API: paths and platform") == "code-api-paths-and-platform"
    assert _slug("История и «Вернуть»") == "история-и-вернуть"
